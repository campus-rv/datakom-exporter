#!/usr/bin/env python3

from pymodbus.client import ModbusTcpClient
import time, json, sys

host = "<controller-ip>"
metrics_file = '/var/lib/prometheus/node-exporter/gen.prom'
step = 16

items = {
  10246: "phase1_voltage",
  10248: "phase2_voltage",
  10250: "phase3_voltage",
  10270: "phase1_current",
  10272: "phase2_current",
  10274: "phase3_current",
  10294: "total_active_power",
#  10310: "total_reactive_power",
  10326: "total_apparent_power",
  10335: "total_power_factor",
  10339: "frequency",
  10341: "battery_voltage",
  10361: "oil_pressure",
  10362: "engine_temp",
  10363: "fuel_level",
  10376: "engine_rpm",
  10604: "operation_status",
  10605: "mode",
  10622: "engine_hours_run",
#  10624: "hours_since_last_service",
#  10626: "days_since_last_service",
#  10628: "total_active_energy",
#  10634: "hours_to_service-1",
#  10636: "days_to_service-1",
#  10638: "hours_to_service-2",
#  10640: "days_to_service-2",
#  10642: "hours_to_service-3",
#  10644: "days_to_service-3"
}

# Calculate bases with step = 16
def get_bases(keys, step=step):
    bases = sorted(set(key // step * step for key in keys))
    return bases

# Extract keys and compute bases
bases = get_bases(items.keys())

#print(json.dumps(items))
#print(json.dumps(bases))
#sys.exit()
data = {}

client = ModbusTcpClient(
    host=host,
    port=502,
    timeout=3
)

client.connect()

# Iterate over each base
for base in bases:
    attempt = 0  # Initialize attempt counter
    max_retries = 4 # Maximum retry count

    while attempt <= max_retries:
        # Read registers starting from 'base' with a step of 16
        request = client.read_holding_registers(base, count=step, slave=1)

        if request.isError():
            print(f"Error reading registers at base {base}, Attempt {attempt + 1}")
            attempt += 1  # Increment retry counter
            if attempt > max_retries:
                print(f"Failed to read registers at base {base} after {max_retries + 1} attempts")
        else:
            # Extract the register values
            register_values = request.registers
            # Map the values to their corresponding items
            for i in range(step):
                index = base + i  # Calculate the current index
                if index in items:  # Check if the index exists in the items dictionary
                    value = register_values[i]  # Get the register value
                    data[items[index]] = value  # Store the value in the data dictionary
            break  # Exit retry loop on success

        time.sleep(1.5)  # Sleep for 1.5 seconds after each attempt or success

# Write the data as Prometheus metrics to a file with the "gen_" prefix
with open(metrics_file, 'w') as f:
    for key, value in data.items():
        if value is not None:
            # Add the "gen_" prefix and make sure the metric name is valid
            metric_name = "gen_" + key.replace(".", "_").replace("-", "_")
            f.write(f"{metric_name} {value}\n")

#print(json.dumps(data))
#with open(json_file, "w") as outfile:
#  json.dump(data, outfile)