#
""""""

from datetime import datetime


def now_iso():
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%f')
