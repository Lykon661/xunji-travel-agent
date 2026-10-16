"""Bound local adapter responses and discard expired entries during cache access."""
from time import monotonic


class AdapterCache(dict):
    def __init__(self, ttl=1800, max_entries=512):
        super().__init__()
        self.ttl,self.max_entries = ttl,max_entries
        self.expires = {}

    def prune(self):
        current = monotonic()
        for key,expiry in list(self.expires.items()):
            if expiry <= current:
                self.pop(key,None)

    def __getitem__(self,key):
        self.prune()
        return super().__getitem__(key)

    def __contains__(self,key):
        self.prune()
        return super().__contains__(key)

    def get(self,key,default=None):
        self.prune()
        return super().get(key,default)

    def __setitem__(self,key,value):
        self.prune()
        if not super().__contains__(key):
            while len(self) >= self.max_entries:
                self.pop(next(iter(self)))
        kind = key[0] if isinstance(key,tuple) and key else None
        ttl = min(self.ttl,{"route":300,"weather":900}.get(kind,self.ttl))
        self.expires[key] = monotonic()+ttl
        super().__setitem__(key,value)

    def pop(self,key,*default):
        self.expires.pop(key,None)
        return super().pop(key,*default)

    def clear(self):
        super().clear()
        self.expires.clear()
