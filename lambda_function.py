import json

from lambda_perf import env_flag, perf_log, wrap_handler

# Record input-complexity attributes alongside the timing.
MEASURE_INPUT = env_flag("PERF_MEASURE_INPUT")


# Set LAMBDA_PERF_INCLUDE_IN_RESPONSE=true to have the decorator add the
# performance record to the response under "lambda_perf" (e.g. so a Step
# Function can read it from the task output).
@wrap_handler(metric_name="my_lambda")
def lambda_handler(event, context):

    if MEASURE_INPUT:
        perf_log.add_attribute("input_size_bytes", len(json.dumps(event, default=str)))
        perf_log.add_attribute("transaction_requested", event.get("transaction"))

    #
    # Your normal Lambda code goes here.
    #

    result = {
        "statusCode": 200,
        "body": "OK",
    }

    return result
