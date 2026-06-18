from foundation.responses import error_response, success_response


class ApiResponse:
    @staticmethod
    def success(data=None, message="操作成功"):
        return success_response(data=data, message=message)

    @staticmethod
    def error(error, status_code=500, **extra_fields):
        return error_response(
            error,
            status_code=status_code,
            **extra_fields,
        )
