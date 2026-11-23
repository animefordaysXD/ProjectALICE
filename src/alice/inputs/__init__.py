"""Input side: sensors, redaction, and the gate that writes the ledger."""

from .files import FileAccessError, FileReader, FileText
from .gate import Gate, IngestResult, Salience, content_hash, normalize
from .redact import RedactionResult, Redactor, redact
from .sensors import PollStats, build_sensors, poll_once
from .sensors.base import Sensor

__all__ = [
    "FileAccessError",
    "FileReader",
    "FileText",
    "Gate",
    "IngestResult",
    "PollStats",
    "RedactionResult",
    "Redactor",
    "Salience",
    "Sensor",
    "build_sensors",
    "content_hash",
    "normalize",
    "poll_once",
    "redact",
]
