"""fhirleaf: FHIR RESTful Search from one SQLite file, without a server (standard library only).

A SQLite file stores the unchanged resources and search index rows extracted
with the official R4 SearchParameter expressions; `Engine.get(path)` answers
type searches, reads, paging and metadata following the FHIR R4 RESTful Search
specification, within the parameters its CapabilityStatement lists. No server,
JVM or database service is needed; `server.serve` is an optional HTTP adapter.
"""
from .search import Engine, SearchError
from .store import load

__version__ = "0.1.1"


def connect(path, base_url="http://localhost/fhir", default_count=None, max_count=None):
    """Open a store read-only. `get(path)` answers like a FHIR server: (HTTP status, JSON).
    default_count / max_count override the page sizes stored at load time."""
    return Engine(path, base_url=base_url, default_count=default_count, max_count=max_count)


__all__ = ["Engine", "SearchError", "connect", "load", "__version__"]
