class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def not_found(what: str = "Not found") -> HttpError:
    return HttpError(404, what)


def forbidden(why: str = "You do not have permission to do this") -> HttpError:
    return HttpError(403, why)


def conflict(why: str, **extra) -> HttpError:
    return HttpError(409, why, **extra)


def bad_request(why: str, **extra) -> HttpError:
    return HttpError(400, why, **extra)
