
import time
from copy import deepcopy
from hdfs import Client
import json
import requests
from requests.auth import HTTPBasicAuth
from pprint import pprint
import os
import re
import yaml
from kubernetes import client, config


def generate_work(num=None):
    url = "https://pony.aidigger.com/api/v1/works"
    res = requests.get(url)
    works = res.json()["data"]
    work_ids = []
    for work in works[::-1]:
        if work["status"] == "RUNNING":
            work_ids.append(work['id'])
    if num:
        return work_ids[:num]
    else:
        return work_ids

def generate_schedule(work_ids):
    schedules_list = []
    for work_id in work_ids:
        schedules_url = "https://pony.aidigger.com/api/v1/works/{}/schedules".format(work_id)
        schedules = requests.get(schedules_url).json()
        if 'data' in schedules.keys():
            schedules = schedules['data']
        else:
            continue
        time.sleep(1)
        for schedule in schedules:
            if (schedule["partition_str"]>"2019-09-00" and schedule["status"]=="SUCCESS") or (schedule["cron_type"]=="STREAM" and schedule["status"]=="RUNNING"):
                schedules_list.append((schedule["work_id"], schedule["id"], schedule["cron_type"]))
                print((schedule["work_id"], schedule["id"], schedule["cron_type"]))
                break
    return schedules_list
    

def generate_job_and_outputtable(schedules_list):
    job_list = []
    outtable_list = []
    client = Client("http://emr2-header-1.ipa.aidigger.com:50070", timeout=30)
    for work_id, schedule_id, cron_type in schedules_list:
        schedule_url = "https://pony.aidigger.com/api/v1/schedules/{}".format(schedule_id)
        job_infos = requests.get(schedule_url).json()["data"]
        time.sleep(1)
        owner = job_infos["owner"]
        print("schedule_url: "+schedule_url)
        for job_info in job_infos["execute_DAG"]:
            try:
                if job_info.get("job_info") \
                    and job_info["job_info"]["configs"].get("command","") \
                    and job_info["job_info"]["configs"]["command"].startswith("data_pipeline") : #or job_info["job_info"]["configs"]["command"].startswith("data_connector")
                    config = job_info["job_info"]["configs"]
                    config['args']["isstreaming"] = str(config['args']["isStreaming"])
                    job_list.append((config["job_id"], job_info["name"], job_info["job_info"]["configs"]["command"], "1G", "0.3", owner, cron_type))
                    for output in config["output"]:
                        outtable_list.append(deepcopy(output))
                        dayu_fullnames = output["dayu_fullname"].split(":")
                        if not dayu_fullnames:
                            raise Exception("error!!")
                        if dayu_fullnames[0].lower() == "hive":
                            dayu_fullnames[1] = "dayu_temp" 
                            output["dayu_full_name"] = ":".join(dayu_fullnames)+"_k8s_press"
                        elif dayu_fullnames[0].lower().startswith("oss"):
                            output["dayu_full_name"] = output["dayu_fullname"][:-1]+"_k8s_press/"
                        else:
                            output["dayu_full_name"] = output["dayu_fullname"]+"_k8s_press"
                        output.pop("dayu_id")
                    content = json.dumps(config).encode(encoding='utf-8')
                    client.write("/tmp/ting.wu/k8s_press/{}.json".format(config["job_id"]), overwrite=True, data=content)
                    print("  hdfs: /tmp/ting.wu/k8s_press/{}.json".format(config["job_id"]))
            except Exception as err:
                print(err)
    return job_list, outtable_list

def copy_job_output_dayu_table(outtable_list):
    failed_table = {}
    for outtable in outtable_list:
        time.sleep(1)
        try:
            table_info = requests.get("https://dayu.aidigger.com/api/v2/tables/{}".format(outtable["dayu_id"])).json()
            table_info.pop("created_at", None)
            table_info.pop("modify_time", None)
            table_info.pop("hash", None)
            table_info.pop("deleted", None)
            table_info.pop("last_modify_time", None)
            table_info.pop("environment", None)
            table_info.pop("file_size", None)
            table_info.pop("tags", None)
            table_info.pop("full_name", None)
            table_info.pop("id", None)
            table_info.pop("data_type", None)
            table_info.pop("project", None)
            table_info.pop("data_managers", None)
            table_info.pop("project_id", None) 
            table_info.pop("es_alias", None) 
            table_info['is_temp']= True
            table_info['owl_id']= 548279
            if table_info["storage"].lower() == "hive":
                table_info['database']= 'dayu_temp'
                table_info['name'] += "_k8s_press"
            elif table_info["storage"].lower() == "es":
                table_info['name'] += '_k8s_press'
                table_info['es_alias'] += '_k8s_press_tmp'
            elif table_info["storage"].lower() == "oss":
                table_info["prefix_pattern"] = table_info["prefix_pattern"].replace(table_info['first_prefix'], table_info['first_prefix']+"_k8s_press")
                table_info['name'] = table_info['name'][:-1]+"_k8s_press/"
                table_info['first_prefix'] += "_k8s_press" 
            else:
                table_info['name'] += "_k8s_press"
            table_info['lifecycle']= -1
            table_info['owner']= '吴婷'
            table_info['alias'] = 'spark on k8s 压测临时表'
            table_info['tags'] = [21]
            table_info['description'] = 'spark on k8s 压测临时表'
            url = "https://dayu.aidigger.com/api/v2/tables"
            res = requests.post(url, json=table_info, auth=HTTPBasicAuth("ting.wu@aidigger.com", "eigen123"))
            if res.status_code ==201:
                print("***"*10+"SUCCESS"+"***"*10)
            else:
                failed_table[str(outtable['dayu_id'])] = outtable
                print('***'*10)
                pprint(res.json())
                print('***'*10)
        except Exception as err:
            print(err)
            failed_table[str(outtable['dayu_id'])] = outtable
    if failed_table:
        # pprint(failed_table)
        print("create dayu table error, {}".format(len(failed_table)))


def press_job_on_k8s(job_list):
    index = 0
    job_folder_name_map = generate_job_folder_name_map(job_list)
    while True:
        save_and_clear_k8s(job_folder_name_map)    
        index = submit_job(job_list, index)
        print("submitted")
        if index >= len(job_list) and not list_running_job():
            print("$$$$$$$$$$ press finished $$$$$$$$")
            break
        save_and_clear_k8s(job_folder_name_map)  

def save_log(pod_name, folder_name, status):
    #os.makedirs("{}-{}".format(folder_name, status), exist_ok=True, mode=0o777)
    log_url = "http://kube-sa.aipp.io/api/v1/log/file/dev/{}/spark-kubernetes-driver?previous=false".format(pod_name)
    log = requests.get(log_url).text
    with open("{}-{}/{}.log".format(folder_name, status, pod_name),'w+') as log_op:
        log_op.write(log)
    print("save {}-{}/{}.log".format(folder_name, status, pod_name))

def save_yaml(yaml_dict, folder_name, status):
    config_yaml = open("{}-{}/{}.yaml".format(folder_name, status, yaml_dict['metadata']['name']),'w+')
    yaml.dump(yaml_dict, config_yaml)
    config_yaml.close()
    print("save: {}-{}/{}.yaml".format(folder_name, status, yaml_dict['metadata']['name']))

def append_log(pod_name, folder_name, status):
    log_url = "http://kube-sa.aipp.io/api/v1/log/dev/{}".format(pod_name)
    log = requests.get(log_url).text
    with open("{}-{}/{}.log".format(folder_name, status, pod_name),'w+') as log_op:
        log_op.write(log)
    print("append history/{}-{}/{}.log".format(folder_name, status, pod_name))

def generate_job_folder_name_map(job_list):
    job_folder_name_map = {}
    for job_id, job_name, command, mem, core,owner,cron_type in job_list:
        folder_name = "history/{}-{}-{}-{}".format(job_name, owner, job_id, cron_type)
        job_folder_name_map[str(job_id)] = folder_name
    return job_folder_name_map 

def save_and_clear_k8s(job_folder_name_map):
    config.load_kube_config('/root/.kube/config')
    configuration = client.Configuration()
    configuration = client.Configuration()
    configuration.verify_ssl=False
    configuration.debug = False
    client.Configuration.set_default(configuration)
    v1 = client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace="dev").items
    for pod in pods:
        time.sleep(1)
        status = pod._status.to_dict()["phase"]
        pod_name = pod.metadata.to_dict()["name"]
        if not pod_name.startswith("appname"):
            continue
        job_id_re = re.search(r"appname(\d+)-", pod_name).groups()
        if not job_id_re:
            continue
        else:
            job_id = job_id_re[0]
        
        if status.lower() in ("failed", "succeeded"):
            os.makedirs("{}-{}".format(job_folder_name_map[job_id], status.lower()), exist_ok=True, mode=0o777)
            save_log(pod_name, job_folder_name_map[job_id], status.lower())
            save_yaml(pod.to_dict(), job_folder_name_map[job_id], status.lower())
            if "exec" in pod_name:
                continue
            v1.delete_namespaced_pod(pod_name, "dev")
            print("deleted pod {}".format(pod_name))
        else:
            os.makedirs("{}-{}".format(job_folder_name_map[job_id], "running"), exist_ok=True, mode=0o777)
            append_log(pod_name, job_folder_name_map[job_id], "running")
            save_yaml(pod.to_dict(), job_folder_name_map[job_id], "running")
            # pod_execs = list_pod_exec(pod_name)
            # for pod_exec in pod_execs:
            #     append_log(pod_exec_name, job_folder_name_map[job_id], "running")
            #     save_yaml(pod_exec.to_dict(), job_folder_name_map[job_id])

def list_running_job():
    config.load_kube_config('/root/.kube/config')
    configuration = client.Configuration()
    configuration = client.Configuration()
    configuration.verify_ssl=False
    configuration.debug = False
    client.Configuration.set_default(configuration)
    v1 = client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace="dev").items
    count = 0
    for pod in pods:
        if pod.metadata.to_dict()["name"].startswith("appname") and pod._status.to_dict()['phase'].lower() in ("pending", "running") and "exec" not in pod.metadata.to_dict()["name"]:
            count += 1
    print("running job {}".format(count))
    return count

def create_pod_yaml(job_info, time_stamp):
    with open("driver.yaml", 'r') as driver_op:
        driver_tmplate = driver_op.read()
    job_id, job_name, command, mem, core,owner,cron_type = job_info
    driver_yaml = driver_tmplate.replace("{appname}", "appname{}-{}".format(job_id, time_stamp))
    driver_yaml = driver_yaml.replace("{appid}", "appid{}-{}".format(job_id, time_stamp))
    driver_yaml = driver_yaml.replace("{command}", "{}".format(command))
    driver_yaml = driver_yaml.replace("{job_id}", str(job_id))
    with open("press/driver.yaml","w+") as op:
        op.write(driver_yaml)


def submit_pod(v1):
    with open("press/driver.yaml", "r") as op:
        driver_pod = yaml.load(op)
    v1.create_namespaced_pod("dev", driver_pod)
    print("submit pod: {}".format(driver_pod["metadata"]["name"]))

def get_pod_uid(job_id, time_stamp):
    url = "http://kube-sa.aipp.io/api/v1/_raw/pod/namespace/dev/name/appname{}-{}".format(job_id,time_stamp)
    res = requests.get(url)
    uid = res.json()["metadata"]["uid"]
    return uid

def create_confmap_service(uid, job_info, time_stamp):
    job_id, job_name, command, mem, core,owner,cron_type = job_info
    with open("confmap.yaml", 'r') as confmap_op:
        confmap_tmplate = confmap_op.read()

    with open("service.yaml", 'r') as service_op:
        service_tmplate = service_op.read()

    confmap_yaml = confmap_tmplate.replace("{appname}", "appname{}-{}".format(job_id,time_stamp))
    confmap_yaml = confmap_yaml.replace("{appid}", "appid{}-{}".format(job_id, time_stamp))
    confmap_yaml = confmap_yaml.replace("{uid}",uid)
    with open("press/confmap.yaml","w+") as op:
        op.write(confmap_yaml)
    service_yaml = service_tmplate.replace("{appname}", "appname{}-{}".format(job_id,time_stamp))
    service_yaml = service_yaml.replace("{appid}", "appid{}-{}".format(job_id,time_stamp))
    service_yaml = service_yaml.replace("{uid}",uid)
    with open("press/service.yaml","w+") as op:
        op.write(service_yaml)

def submit_confmap_service(v1):
    with open("press/confmap.yaml", "r") as op:
        confmap = yaml.load(op)
    v1.create_namespaced_config_map("dev", confmap)
    print("submit confmap: {}".format(confmap["metadata"]["name"]))

    with open("press/service.yaml", "r") as op:
        service = yaml.load(op)
    v1.create_namespaced_service("dev", service)
    print("submit service: {}".format(service["metadata"]["name"]))

def submit_job(job_list, index_start):
    config.load_kube_config('/root/.kube/config')
    configuration = client.Configuration()
    configuration = client.Configuration()
    configuration.verify_ssl=False
    configuration.debug = False
    client.Configuration.set_default(configuration)
    v1 = client.CoreV1Api()
    running_num = list_running_job()
    end = index_start+(10-running_num)
    if end>len(job_list):
        end = len(job_list)
    print("submitting index: from {} to {}".format(index_start, end))
    for job_info in job_list[index_start: end]:
        time.sleep(2)
        time_stamp = int(time.time())
        create_pod_yaml(job_info, time_stamp)
        submit_pod(v1) 
        job_id = str(job_info[0])
        uid = get_pod_uid(job_id, time_stamp)
        create_confmap_service(uid, job_info, time_stamp)
        submit_confmap_service(v1)
    return end

def main():
    limit = 0
    print("***"*10+"generate work id"+"***"*10)
    work_ids = generate_work(limit)
    print(len(work_ids))
    print("***"*10+"generate_schedule id"+"***"*10)
    schedules_list = generate_schedule(work_ids)
    print(len(schedules_list))
    print("***"*10+"generate_job_and_outputtable"+"***"*10)
    job_list, outtable_list = generate_job_and_outputtable(schedules_list)
    print("job_list: "+str(len(job_list)))
    print("outtable_list: "+str(len(outtable_list)))
    print("***"*10+"copy_job_output_dayu_table"+"***"*10)
    copy_job_output_dayu_table(outtable_list)
    print(job_list)

    #job_list = [(1725174, 'Hive2Hive', 'data_pipeline.plugin_base.data_transmit.DataTransmit.hive2hive', '1G', '0.3', 'linqiaosheng', 'WEEK'), (1725195, 'Hive2Socrates_1', 'data_pipeline.plugin_base.socrates_plugin.SocratesPlugin.hive2socrates', '1G', '0.3', 'linqiaosheng', 'WEEK'), (1725168, 'Oss2Parsed', 'data_pipeline.plugin_base.common_parsed.CommonParsedPlugin.run', '1G', '0.3', 'linqiaosheng', 'WEEK'), (1720648, 'CommonParsedPlugin', 'data_pipeline.plugin_base.common_parsed.CommonParsedPlugin.run', '1G', '0.3', 'herui', 'DAY'), (1717256, 'DayuES2Hive', 'data_pipeline.plugin_base.elasticsearch2hive_plugin.Elasticsearch2HivePlugin.run', '1G', '0.3', 'tulingfeng', 'DAY')]
    print("***"*10+"press_job_on_k8s"+"***"*10)
    press_job_on_k8s(job_list)

main()
