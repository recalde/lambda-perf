from lambda_perf import wrap_handler


@wrap_handler(metric_name="my_lambda")
def lambda_handler(event, context):

    #
    # Your normal Lambda code goes here.
    #

    result = {
        "statusCode": 200,
        "body": "OK",
    }

    return result