"""Safe boundary errors: never expose a driver exception, statement or submitted value."""


class PersistenceError(RuntimeError):
    pass


class PersistenceConflict(PersistenceError):
    pass


class PersistenceUnavailable(PersistenceError):
    pass


class RecordNotFound(PersistenceError):
    pass
