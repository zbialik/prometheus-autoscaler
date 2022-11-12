#!/usr/bin/env python3

import boto3
import requests
import json
import os
import yaml
import shutil
import logging
from logging.handlers import RotatingFileHandler

# extract OS envs
OBJSTORE_CONFIG = yaml.load(os.environ['OBJSTORE_CONFIG'], Loader=yaml.SafeLoader)
S3_BUCKET = OBJSTORE_CONFIG['config']['bucket']

# set constants
S3_CLIENT = boto3.client('s3')
PROMETHEUS_DIRECTORY="/prometheus"
PROMETHEUS_CONF_OUT_YAML="/etc/prometheus/config_out/prometheus.env.yaml"

def cleanup():
    removeDirs = ['snapshots', 'wal'] # TODO: consider wiping out everything under /prometheus
    for d in removeDirs:
        if os.path.isdir(d):
            logging.debug(f"removing directory {PROMETHEUS_DIRECTORY}/{d}")
            shutil.rmtree(f"{PROMETHEUS_DIRECTORY}/{d}")

def init_thanos_meta() -> dict:
    # read outputted prometheus config for initializing metadata
    logging.debug(f"reading metadata in {PROMETHEUS_CONF_OUT_YAML}")
    with open(PROMETHEUS_CONF_OUT_YAML,'r') as stream:
        promconf = yaml.load(stream, Loader=yaml.SafeLoader)
    
    labels = promconf['global']['external_labels']
    labels['prom_shard'] = f"{labels['prom_shard']}-manual-snapshot-upload"
    thanos:dict = {
        "labels": labels,
        "downsample": {
            "resolution": 0
        },
        "source": "sidecar",
        "segment_files": [],
        "files": [
            {
                "rel_path": "meta.json"
            }
        ]
    }
    return thanos

def request_tsdb_snapshot(snapshotUrl:str='http://localhost:9090/api/v1/admin/tsdb/snapshot') -> str:
    res = requests.post(snapshotUrl, json = {})
    logging.debug(f"response json:\n {json.dumps(res.json(),indent=4)}")
    return f"{PROMETHEUS_DIRECTORY}/snapshots/{res.json()['data']['name']}"

def setup_logger():
    for name in ['boto', 'urllib3', 's3transfer', 'boto3', 'botocore', 'nose']: # disable noisy loggers
        logging.getLogger(name).setLevel(logging.CRITICAL)
    logging.basicConfig(
        format='%(asctime)s %(message)s', 
        encoding='utf-8', 
        level=logging.DEBUG,
        handlers=[RotatingFileHandler(f"{PROMETHEUS_DIRECTORY}/snapshot-uploader.log", maxBytes=500000, backupCount=5)]
    )

def upload_snapshot_blocks(snapshotDir:str):
    def upload_block(snapshotDir:str, blockDir:str, chunks:list()):
        logging.debug(f"uploading blockDir: {blockDir}")
        uploadFiles = [
            f"{blockDir}/index",
            f"{blockDir}/meta.json",
        ]
        uploadFiles += [f"{blockDir}/chunks/" + chunk for chunk in chunks]

        for file in uploadFiles:
            objKey = file.removeprefix(f"{snapshotDir}/")
            with open(file, 'rb') as data:
                S3_CLIENT.upload_fileobj(data, S3_BUCKET, objKey) # NOTE: boto3 doesn't support sync at the moment
    
    for block in os.listdir(snapshotDir):
        blockDir = f"{snapshotDir}/{block}"

        # read meta.json to metadata
        with open(f"{blockDir}/meta.json") as metafile:
            metadata = json.load(metafile)
        
        # init thanos field in metadata
        metadata['thanos'] = init_thanos_meta()

        # add index size
        metadata['thanos']['files'].append({
            "rel_path": "index",
            "size_bytes": os.path.getsize(f"{blockDir}/index")
        })

        chunks = os.listdir(f"{blockDir}/chunks")
        for chunk in chunks:
            metadata['thanos']['segment_files'].append(str(chunk))
            metadata['thanos']['files'].append({
                "rel_path": f"chunks/{chunk}",
                "size_bytes": os.path.getsize(f"{blockDir}/chunks/{chunk}")
            })
        
        # overwrite meta.json
        with open(f"{blockDir}/meta.json", "w") as metafile:
            json.dump(metadata, metafile, indent=3)
        
        # upload block
        upload_block(snapshotDir, blockDir, chunks)

def main():
    # setup logger
    setup_logger() 

    # take TSDB snapshot using Prometheus HTTP API
    snapshotDir = request_tsdb_snapshot() 

    # update each block's meta.json for thanos data and then upload block to S3
    upload_snapshot_blocks(snapshotDir) 
    
    # wipe out relevant folders/files (e.g. /prometheus/snapshots)
    cleanup()

if __name__ == '__main__':
    main()
