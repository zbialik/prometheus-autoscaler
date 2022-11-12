import asyncio
from datetime import datetime
from decimal import Decimal
from math import ceil
import kopf
import os

# local imports
from kube import KubeClient
import utils

# KOPF PARAMETERS
PROM_CRD = {
    'group': "monitoring.coreos.com",
    'version': "v1",
    'plural': "prometheuses"
}

# CUSTOM PARAMETERS
PROM_AUTOSCALER_DAEMON_DELAY = int(os.getenv('PROM_AUTOSCALER_DAEMON_DELAY', '0')) # time to delay daemon start when operator startsup OR an autoscaling Prometheus is created
PROM_AUTOSCALER_KEY_PREFIX = os.getenv('PROM_AUTOSCALER_KEY_PREFIX', 'prom-shard-autoscaling.zbialikcloud.io') 
PROM_AUTOSCALER_TIMESTAMP_ANNOTATION_KEY = f"{PROM_AUTOSCALER_KEY_PREFIX}/scale-time"

# CONSTANTS
EVALUATION_INTERVAL = 5 # seconds
LOGGER = None

@kopf.on.startup()
def configure(logger, settings: kopf.OperatorSettings, **_):
    settings.persistence.finalizer = f"{PROM_AUTOSCALER_KEY_PREFIX}/finalizer"
    global LOGGER
    LOGGER = logger

# DAEMON FOR AUTOSCALING PROMS WITH ANNOTATION: prom-shard-autoscaling.zbialikcloud.io/enable: 'true'
@kopf.daemon(PROM_CRD['group'], PROM_CRD['version'], PROM_CRD['plural'], 
    annotations={f"{PROM_AUTOSCALER_KEY_PREFIX}/enable": 'true'},
    initial_delay=PROM_AUTOSCALER_DAEMON_DELAY
)
async def prom_scaler_async(spec, name, namespace, annotations, labels, patch, **kwargs):
    
    # init local constants
    kubeclient = KubeClient(logger=LOGGER)
    countErrorMax = 5

    # init variables
    countError = 0
    countWarmup = 0
    prevDesiredShards = 0
    configs = get_autoscaling_configs(annotations)

    while True:
        try:
            configs = get_autoscaling_configs(annotations, configs)
            
            # cooldown after previous scale event
            await cooldown(kubeclient, name, namespace, configs['min-cooldown'], annotations)

            # main shard analysis and update sequence
            prevDesiredShards, countWarmup = scale_sequence(
                kubeclient, 
                name, 
                namespace, 
                spec,
                prevDesiredShards, 
                countWarmup, 
                configs
            )
            countError = 0
        
        except Exception as e:
            countError += 1
            if countError == countErrorMax:
                raise Exception(f"max errors allowed in main() loop reached ({countErrorMax})")
            else:
                LOGGER.error(f"exception caught in in main() loop: {e}")
                LOGGER.warning(f"{countError} error(s) occurred back to back in main() loop out of {countErrorMax} allowed")
        finally:
            await asyncio.sleep(EVALUATION_INTERVAL)

def scale_sequence(kubeclient:KubeClient, name:str, namespace:str, spec,
        prevDesiredShards:int, 
        countWarmup:int,
        configs:dict
    ):
    # calculate desired shards
    desiredShards = kubeclient.calculate_desired_shards(name, namespace, spec, 
        configs['min-shards'],
        configs['max-shards'], 
        disableScaleDown=configs['disable-scale-down'],
        algorithm=configs['desired-shards-algorithm'],
        usageCalculator=configs['current-usage-calculator'],
        targetUtil=configs['target-memory-util'],
        targetUtilScaleUp=configs['target-memory-util-scale-up'],
        targetUtilScaleDown=configs['target-memory-util-scale-down'],
        minDecrement=configs['min-decrement'],
        minIncrement=configs['min-increment'],
        maxDecrement=configs['max-decrement'],
        maxIncrement=configs['max-increment'],
    )
    
    # calculate minimum iterations before executing a scaleUp or scaleDown
    countWarmupScaleUpMin = ceil(configs['min-warmup-scale-up'] / EVALUATION_INTERVAL)
    countWarmupScaleDownMin = ceil(configs['min-warmup-scale-down'] / EVALUATION_INTERVAL)
    
    if desiredShards == spec['shards']: # desired matches current
        LOGGER.info(f"desiredShards matches current ({spec['shards']})")
        countWarmup = 0
    elif desiredShards != prevDesiredShards: # desired doesn't match current AND doesn't match previous
        LOGGER.info(f"desiredShards ({desiredShards}) has changed from previous evaluation ({prevDesiredShards})")
        prevDesiredShards = desiredShards
        countWarmup = 0
    elif desiredShards > spec['shards']: # desired is greater than current AND matches previous 
        if countWarmup == countWarmupScaleUpMin:
            kubeclient.scale_prom_shards(name, namespace, PROM_CRD, desiredShards, PROM_AUTOSCALER_TIMESTAMP_ANNOTATION_KEY)
            countWarmup = 0
        else:
            LOGGER.info(f"waiting {countWarmupScaleUpMin - countWarmup} more {EVALUATION_INTERVAL}s loops before executing shard patch")
            countWarmup += 1
    elif desiredShards < spec['shards']: # desired is less than current AND matches previous
        if countWarmup == countWarmupScaleDownMin:
            kubeclient.scale_prom_shards(name, namespace, PROM_CRD, desiredShards, PROM_AUTOSCALER_TIMESTAMP_ANNOTATION_KEY)
            countWarmup = 0
        else:
            LOGGER.info(f"waiting {countWarmupScaleDownMin - countWarmup} more {EVALUATION_INTERVAL}s loops before executing shard patch")
            countWarmup += 1
    else: # TODO: remove after proving impossible to reach
        LOGGER.warning(f"entered a conditional block that should not have been possible")
        countWarmup = 0
    return prevDesiredShards, countWarmup

async def cooldown(kubeclient:KubeClient, name, namespace, minCooldownPeriod, annotations:dict):
    LOGGER.info("determining time to cooldown since last scale")
    
    if PROM_AUTOSCALER_TIMESTAMP_ANNOTATION_KEY not in annotations.keys():
        LOGGER.info(f"timestamp annotation does not exist on object.")
        kubeclient.add_timestamp_annotation(name, namespace, PROM_CRD, PROM_AUTOSCALER_TIMESTAMP_ANNOTATION_KEY)
    else:
        prevTimestamp = float(annotations[PROM_AUTOSCALER_TIMESTAMP_ANNOTATION_KEY])
        LOGGER.debug(f"previous update occurred at timestamp: {prevTimestamp}")
        timedelta = datetime.now() - datetime.fromtimestamp(prevTimestamp)
        secondsSincePatch = float(timedelta.total_seconds())
        
        cooldownSeconds = minCooldownPeriod - secondsSincePatch
        if cooldownSeconds > 0:
            await utils.sleep_and_log(cooldownSeconds, LOGGER)

def get_autoscaling_configs(annotations:dict, preConfigs:dict={}):
    def log_config_settings():
        LOGGER.info(f"prometheus reloaded with with following autoscaling configs:")
        for key,value in configs.items():
            LOGGER.info(f"\t {key} = {str(value)}")
    
    configs = { # defaults from os env
        'disable-scale-down': utils.stringToBool(os.getenv('PROM_AUTOSCALER_DISABLE_SCALE_DOWN', 'false')),
        'min-shards': int(os.getenv('PROM_AUTOSCALER_MIN_SHARDS', '1')),
        'max-shards': int(os.getenv('PROM_AUTOSCALER_MAX_SHARDS', '7')),
        'target-memory-util': Decimal(os.getenv('PROM_AUTOSCALER_TARGET_MEM_UTIL', '0.75')),
        'min-warmup-scale-up': int(os.getenv('PROM_AUTOSCALER_MIN_WARMUP_SCALE_UP', '60')),
        'min-warmup-scale-down': int(os.getenv('PROM_AUTOSCALER_MIN_WARMUP_SCALE_DOWN', '1800')),
        'min-cooldown': int(os.getenv('PROM_AUTOSCALER_MIN_COOLDOWN', '1800')),
        'desired-shards-algorithm': os.getenv('PROM_AUTOSCALER_DESIRED_SHARDS_ALOGORITHM', 'double-or-decrement'),
        'current-usage-calculator': os.getenv('PROM_AUTOSCALER_CURR_USAGE_CALCULATOR', 'avg'),
        'target-memory-util-scale-up': Decimal(os.getenv('PROM_AUTOSCALER_TARGET_MEM_UTIL_SCALE_UP', '0.75')),
        'target-memory-util-scale-down': Decimal(os.getenv('PROM_AUTOSCALER_TARGET_MEM_UTIL_SCALE_DOWN', '0.25')),
        'min-decrement': int(os.getenv('PROM_AUTOSCALER_MIN_DECREMENT', '0')),
        'min-increment': int(os.getenv('PROM_AUTOSCALER_MIN_INCREMENT', '0')),
        'max-decrement': int(os.getenv('PROM_AUTOSCALER_MAX_DECREMENT', '0')),
        'max-increment': int(os.getenv('PROM_AUTOSCALER_MAX_INCREMENT', '0'))
    }

    for key, value in configs.items(): # override defaults with configs from annotations
        t = type(value) # need to enforce type
        if t is bool:
            configs[key] = utils.stringToBool(annotations.get(f"{PROM_AUTOSCALER_KEY_PREFIX}/{key}", str(value)))
        else:
            configs[key] = t(annotations.get(f"{PROM_AUTOSCALER_KEY_PREFIX}/{key}", value))
    
    if configs != preConfigs:
        log_config_settings()
    
    return configs
