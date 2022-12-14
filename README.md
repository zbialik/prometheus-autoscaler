# Scale Prometheus Shards

A controller for `Prometheus` objects that have the annotation: `prom-shard-autoscaling.zbialik.io/enable: true` (the annotation key is customizable).

The controller will watch the **memory usage** of the prometheus `Pods` and update the Prometheus `spec.shards` field using a defined autoscaling algorithm.

## Autoscaling Algorithms

The initial release provides 2 choices for the autoscaling algorithm to leverage for a `Prometheus`:

| algorithm | description |
| ------ | ------ |
| `hpa` | `desiredShards = ceil(currShards * ( currMemory / desiredMemory ))` |
| `double-or-decrement` | **double shards** if `currUtil > targetScaleUpUtil` OR **decrement shards** by some amount if `currUtil < targetScaleDownUtil` |

I've set the `double-or-decrement` as the default algorithm used by the controller.

## Autoscaling Config Settings

The controller is configured using `annotations` on the `Prometheus` CRD object with autoscaling enabled. See the annotation examples below:

```yaml
metadata:
  annotations:
    prom-shard-autoscaling.zbialik.io/enable: 'true'
    prom-shard-autoscaling.zbialik.io/disable-scale-down: 'false'
    prom-shard-autoscaling.zbialik.io/min-shards: '1'
    prom-shard-autoscaling.zbialik.io/max-shards: '7'
    prom-shard-autoscaling.zbialik.io/min-warmup-scale-up: '60'
    prom-shard-autoscaling.zbialik.io/min-warmup-scale-down: '1800'
    prom-shard-autoscaling.zbialik.io/min-cooldown: '1800'
    prom-shard-autoscaling.zbialik.io/desired-shards-algorithm: 'double-or-decrement'
    prom-shard-autoscaling.zbialik.io/current-usage-calculator: 'avg'
    prom-shard-autoscaling.zbialik.io/target-memory-util-scale-up: '0.9'
    prom-shard-autoscaling.zbialik.io/target-memory-util-scale-down: '0.5'
    prom-shard-autoscaling.zbialik.io/min-decrement: '0'
    prom-shard-autoscaling.zbialik.io/min-increment: '0'
    prom-shard-autoscaling.zbialik.io/max-decrement: '0'
    prom-shard-autoscaling.zbialik.io/max-increment: '0'
```

You can also set the default config settings using `envVars` set on the `prometheus-autoscaler`. These are shown below:

| ENV | Description | Default |
| ------ | ------ | ------ |
| `PROM_AUTOSCALER_DAEMON_DELAY` | time to delay daemon start when operator startsup OR an autoscaling Prometheus is created | `'0'` |
| `PROM_AUTOSCALER_MIN_SHARDS` | min shards allowed for a `Prometheus` | `'1'` |
| `PROM_AUTOSCALER_MAX_SHARDS` | max shards allowed for a `Prometheus` | `'7'` |
| `PROM_AUTOSCALER_MIN_WARMUP_SCALE_UP` | time that desiredShards must be at a NEW value before scaling UP shards | `'60'` |
| `PROM_AUTOSCALER_MIN_WARMUP_SCALE_DOWN` | time that desiredShards must be at a NEW value before scaling DOWN shards | `'900'` |
| `PROM_AUTOSCALER_MIN_COOLDOWN` | the amount of time after executing patch to wait until next evaluation period | `'900'` |
| `PROM_AUTOSCALER_KEY_PREFIX` | annotation/finalizer prefix for controller to use when controlling `Prometheus` objects | `'prom-shard-autoscaling.zbialik.io'` |
| `PROM_AUTOSCALER_DESIRED_SHARDS_ALOGORITHM` | algorithm to use for calculating the desired shards for a `Prometheus` | `'double-or-decrement'` |
| `PROM_AUTOSCALER_TARGET_MEM_UTIL` | target memory utilization when using the `hpa` algorithm | `'0.5'` |
| `PROM_AUTOSCALER_TARGET_MEM_UTIL_SCALE_UP` | target memory utilization to trigger scale UP event when using the `double-or-decrement` algorithm  | `'0.75'` |
| `PROM_AUTOSCALER_TARGET_MEM_UTIL_SCALE_DOWN` | target memory utilization to trigger scale DOWN event when using the `double-or-decrement` algorithm | `'0.25'` |
| `PROM_AUTOSCALER_MIN_DECREMENT` | min amount that the shards must be decremented during scale DOWN event | `'0'` (disabled) |
| `PROM_AUTOSCALER_MIN_INCREMENT` | min amount that the shards must be incremented during scale UP event | `'0'` (disabled) |
| `PROM_AUTOSCALER_MAX_DECREMENT` | max amount that the shards can be decremented during scale DOWN event | `'0'` (disabled) |
| `PROM_AUTOSCALER_MAX_INCREMENT` | max amount that the shards can be incremented during scale UP event | `'0'` (disabled) |

## Usage

### Local

Initialize `python` virtual environment and upgrade `pip`

```bash
python3 -m venv ~/venv/prometheus-shard-autoscaler
~/venv/prometheus-shard-autoscaler/bin/pip install --upgrade pip
```

Install `pip` packages in `requirements.txt`

```bash
~/venv/prometheus-shard-autoscaler/bin/pip install --no-cache-dir -r requirements.txt
```

Run code

```bash
~/venv/prometheus-shard-autoscaler/bin/kopf run prometheus_shard_autoscaler/app.py --all-namespaces
```

### Kubernetes

You can deploy the `prometheus-autoscaler` using the `kustomize` manifests found in [examples/prometheus-autoscaler](examples/prometheus-autoscaler)

## Thanos Sidecar Integration

### TSDB Upload Problem

When using [Thanos Sidecar](https://thanos.io/tip/components/sidecar.md/) as part of a `Prometheus` deployment, we must manually upload a TSDB block to the cloud object store (e.g. S3) on downscale. This is because the sidecar only uploads blocks every 2 hrs, by design. If we do not perform the manual upload, we risk losing 2 hrs of metrics for the given shard.

### TSDB Upload Solution

Since `Prometheus` `v2.1`, we can now take TSDB Snapshots using `Prometheus` [TSDB Admin API](https://prometheus.io/docs/prometheus/latest/querying/api/#tsdb-admin-apis).

As such, we can take a TSDB Snapshot as part of a `Prometheus` shutdown procedure and then manually upload the snapshotted block to the given cloud object store.
- Note: we must update the `index` file with the required `thanos` metadata for [Thanos StoreGateway](https://thanos.io/tip/components/store.md) to leverage.

I have written a script that does the above for an S3 object store. The script expects to be executed as a sidecar in a `Prometheus` `Pod` running in `Kubernetes`. I leverage a `preStop` lifecycle hook to ensure the script is executed as part of the `Pod`'s shutdown. 

You can find it's example usage in the `kustomize` manifests in [examples/prometheus](examples/prometheus)
