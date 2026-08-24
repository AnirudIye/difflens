"""One rule about user-supplied text, in the one place both routers can reach.

Postgres text columns cannot hold U+0000: the driver refuses the parameter
outright. A NUL arriving in a request body therefore turns an ordinary insert
into a server error, which is both a lie (the caller sent bad input) and a
free way for an anonymous caller to fill the error log.

Stripping rather than rejecting is deliberate. Nobody types a NUL on purpose,
so there is no message whose meaning depends on one, and a paste that happens
to carry one should still reach us rather than bouncing off a validation error
the sender cannot see or explain.
"""


def strip_nul(value: object) -> object:
    """Remove NUL characters from a string, passing anything else through."""
    return value.replace("\x00", "") if isinstance(value, str) else value
