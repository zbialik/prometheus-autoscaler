from decimal import Decimal
from math import ceil
import utils
from kubernetes import client, config
from datetime import datetime

PROM_OPERATOR_LABEL_PREFIX="operator.prometheus.io"

class KubeClient:
    def __init__(self, logger, kubeconfig = None):
        try:
            if not kubeconfig:
                config.load_incluster_config()
            else:
                config.load_kube_config(kubeconfig)
        except config.ConfigException:
            config.load_kube_config()
        
        self.clientCustomObjectsApi = client.CustomObjectsApi()
        global LOGGER
        LOGGER = logger
            
    def prom_pod_usage(self, name:str, namespace:str, usageCalculator:str):
        def prom_pod_usage_avg():
            LOGGER.debug(f"calculating current resource usage for {name} prometheus")
            sumCpu = 0; sumMemory = 0; availablePodCount = 0
            metricList = self.clientCustomObjectsApi.list_namespaced_custom_object(
                group = 'metrics.k8s.io', 
                version = 'v1beta1', 
                plural = 'pods',
                namespace=namespace,
                field_selector = f"metadata.namespace={namespace}",
                label_selector = f"{PROM_OPERATOR_LABEL_PREFIX}/name={name}"
            )

            for pod in metricList['items']:
                cpu = 0; memory = 0
                for container in pod['containers']:
                    usage = container['usage']
                    cpu += utils.parse_quantity(usage['cpu'])
                    memory += utils.parse_quantity(usage['memory'])
                if cpu != 0 and memory != 0: # ignore pods that show 0 usage (maybe cause they're pending/scheduling)
                    availablePodCount += 1
                    sumCpu += cpu
                    sumMemory += memory
            
            # calculate average usage
            if availablePodCount == 0:
                return {'cpu': 0, 'memory': 0}
            else:
                avgCpu = sumCpu / availablePodCount
                avgCpu = sumMemory / availablePodCount
                return {'cpu': avgCpu, 'memory': avgCpu}
            
        def prom_pod_usage_max():
            LOGGER.debug(f"calculating current resource usage for {name} prometheus")
            maxCpu = 0; maxMemory = 0
            metricList = self.clientCustomObjectsApi.list_namespaced_custom_object(
                group = 'metrics.k8s.io', 
                version = 'v1beta1', 
                plural = 'pods',
                namespace=namespace,
                field_selector = f"metadata.namespace={namespace}",
                label_selector = f"{PROM_OPERATOR_LABEL_PREFIX}/name={name}"
            )
            
            for pod in metricList['items']:
                cpu = 0; memory = 0;
                for container in pod['containers']:
                    usage = container['usage']
                    cpu += utils.parse_quantity(usage['cpu'])
                    memory += utils.parse_quantity(usage['memory'])
                maxCpu = max(maxCpu, cpu)
                maxMemory = max(maxMemory, memory)
            
            return {'cpu': maxCpu, 'memory': maxMemory}
        
        if usageCalculator == 'max':
            return prom_pod_usage_max()
        elif usageCalculator == 'avg':
            return prom_pod_usage_avg()
        else:
            raise Exception(f"provided usageCalculator, {usageCalculator}, must be 'max' or 'avg'")

    def calculate_desired_shards(self, name:str, namespace:str, spec, 
        minShards:int, maxShards:int, 
        disableScaleDown:bool=False,
        algorithm:str='double-or-decrement', 
        usageCalculator:str='avg', 
        targetUtil:Decimal = 1.0, 
        targetUtilScaleUp:Decimal = 0.75, # only used for algorithm='thresholds'
        targetUtilScaleDown:Decimal = 0.25,  # only used for algorithm='thresholds'
        minDecrement:int=0, # 0 means disable
        minIncrement:int=0, # 0 means disable
        maxDecrement:int=0, # 0 means disable
        maxIncrement:int=0  # 0 means disable
    ) -> int:
        def enforce_thresholds(desiredShards) -> int:
            # enforce configured thresholds for min/max step-up and step-down
            step = desiredShards - spec['shards']
            if step > 0: # scale-up
                if minIncrement > 0 and step < minIncrement:
                    LOGGER.debug(f"calculated desiredShards produces scale-up less than minIncrement ({minIncrement}) - updating to satisfy minIncrement")
                    desiredShards = spec['shards'] + minIncrement
                elif maxIncrement > 0 and step > maxIncrement:
                    LOGGER.debug(f"calculated desiredShards produces scale-up more than maxIncrement ({maxIncrement}) - updating to satisfy maxIncrement")
                    desiredShards = spec['shards'] + maxIncrement
            elif step < 0: # scale-down
                if disableScaleDown:
                    LOGGER.debug(f"calculated desiredShards is less than current but scale-down is disabled - setting to current spec")
                    desiredShards = spec['shards']
                else:
                    if minDecrement > 0 and abs(step) < minDecrement:
                        LOGGER.debug(f"calculated desiredShards produces scale-down less than minDecrement ({minDecrement}) - updating to satisfy minDecrement")
                        desiredShards = spec['shards'] - minDecrement
                    elif maxDecrement > 0 and abs(step) > maxDecrement:
                        LOGGER.debug(f"calculated desiredShards produces scale-down more than maxDecrement ({maxDecrement}) - updating to satisfy maxDecrement")
                        desiredShards = spec['shards'] - maxDecrement
            
            if desiredShards > maxShards:
                LOGGER.debug(f"calculated desiredShards is greater than maxShards ({maxShards}) - updating to satisfy maxShards")
                desiredShards = maxShards
            elif desiredShards < minShards:
                LOGGER.debug(f"calculated desiredShards is less than minShards ({minShards}) - updating to satisfy minShards")
                desiredShards = minShards
            
            LOGGER.info(f"desiredShards for {name} prometheus is {desiredShards}")
            return desiredShards
        
        def desired_shards_hpa() -> int:
            LOGGER.info(f"{name} prometheus has current shards: {spec['shards']}")
            
            # GET CURRENT USAGE
            usage = self.prom_pod_usage(name, namespace, usageCalculator)
            memCurr = usage['memory']
            if memCurr == 0: # dont produce downscale if memCurr is 0
                LOGGER.warning("current memory usage returned 0 bytes! - is metrics api available?")
                LOGGER.info("setting desired to current")
                return spec['shards']
            
            LOGGER.info(f"{name} prometheus has current memory: {utils.sizeof_fmt(memCurr)}")

            # GET TARGET MEMORY
            memTarget = utils.parse_quantity(spec['resources']['requests']['memory']) * targetUtil
            LOGGER.info(f"{name} prometheus has target memory: {utils.sizeof_fmt(memTarget)}")

            desiredShards = ceil(spec['shards'] * ( memCurr / memTarget )) # using traditional HPA algorithm
            
            # return after enforcing threshold settings
            return enforce_thresholds(desiredShards)
        
        def desired_shards_double_or_decrement() -> int:
            LOGGER.info(f"{name} prometheus has current shards: {spec['shards']}")
            
            # GET CURRENT USAGE
            usage = self.prom_pod_usage(name, namespace, usageCalculator)
            memCurr = usage['memory']
            if memCurr == 0: # dont produce downscale if memCurr is 0
                LOGGER.warning("current memory usage returned 0 bytes! - is metrics api available?")
                LOGGER.info("setting desired to current")
                return spec['shards']
            
            memTarget = utils.parse_quantity(spec['resources']['requests']['memory']) 
            memCurrUtil = (memCurr / memTarget)
            LOGGER.info(f"{name} prometheus has current memory util {memCurrUtil:.3f}")

            if memCurrUtil > targetUtilScaleUp: # double shards
                LOGGER.debug(f"{name} memory util is greater than target for scale-up ({targetUtilScaleUp:.3f}) - desired is double current value")
                desiredShards = spec['shards'] * 2
            elif memCurrUtil < targetUtilScaleDown: # decrement by 1
                LOGGER.debug(f"{name} memory util is less than target for scale-down ({targetUtilScaleDown:.3f}) - desired is current value minus minDecrement")
                desiredShards = spec['shards'] - 1
            else: # keep current
                LOGGER.debug(f"{name} memory util is withing defined thresholds for scaling - desired is current value")
                desiredShards = spec['shards']
            # return after enforcing threshold settings
            return enforce_thresholds(desiredShards)
        
        if algorithm == 'hpa':
            LOGGER.debug(f"calculating desiredShards with algorithm=hpa")
            return desired_shards_hpa()
        elif algorithm == 'double-or-decrement':
            LOGGER.debug(f"calculating desiredShards with algorithm=double-or-decrement")
            return desired_shards_double_or_decrement()
        else:
            raise Exception(f"provided algorithm, {algorithm}, must be 'hpa' or 'double-or-decrement'")
    
    def scale_prom_shards(self, name:str, namespace:str, promCrd:dict, desiredShards:int, annotationKey:str):
        LOGGER.info(f"patching {name} prometheus shards to {desiredShards}")
        body = {
            'metadata': {
                'annotations': {
                    f"{annotationKey}": str(datetime.now().timestamp())
                }
            },
            'spec': {
                'shards': desiredShards
            }
        }
        self.clientCustomObjectsApi.patch_namespaced_custom_object(
            group = promCrd['group'], 
            version = promCrd['version'], 
            plural = promCrd['plural'], 
            namespace=namespace, 
            name = name, 
            body = body
        )

    def add_timestamp_annotation(self, name:str, namespace:str, promCrd:dict, annotationKey:str):
        LOGGER.info(f"patching {name} prometheus with current timestamp annotation")
        body = {
            'metadata': {
                'annotations': {
                    f"{annotationKey}": str(datetime.now().timestamp())
                }
            }
        }
        self.clientCustomObjectsApi.patch_namespaced_custom_object(
            group = promCrd['group'], 
            version = promCrd['version'], 
            plural = promCrd['plural'], 
            namespace=namespace, 
            name = name, 
            body = body
        )
