"""fhirlite: an embedded FHIR R4 search engine on one SQLite file (standard library only).

A SQLite file stores the unchanged resources and search index rows extracted
with the official R4 SearchParameter expressions; `Engine.get(path)` answers
type searches, reads, paging and metadata following the FHIR R4 RESTful Search
specification, within the parameters its CapabilityStatement lists. No server,
JVM or database service is needed; `server.serve` is an optional HTTP adapter.
"""
from .search import Engine, SearchError
from .store import load

__version__ = "0.3.0"


def connect(path, base_url="http://localhost/fhir", default_count=None, max_count=None, writable=False,
            definitions=None, packages=(), clock=None):
    """Open a store. `get(path)` answers like a FHIR server: (HTTP status, JSON).
    default_count / max_count override the page sizes stored at load time.
    writable=True also answers `post` (create), `put` and `delete`. It needs the definitions and
    packages the store was built with. clock: a function returning the current UTC datetime
    (default: the system clock), for reproducible writes. Close a writable store with `close()`."""
    return Engine(path, base_url=base_url, default_count=default_count, max_count=max_count, writable=writable,
                  definitions=definitions, packages=packages, clock=clock)


__all__ = ["Engine", "SearchError", "connect", "load", "__version__"]
