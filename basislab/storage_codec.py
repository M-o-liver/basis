"""Lossless tape encodings. These change representation, never reducer inputs."""
import copy
import hashlib
import json
import math
import zlib
from .store import encode, decode

IDENTIFIERS = {'asset_id','token_id','tokenId','market','instrument_name','instrument',
               'description','question','image','icon','resolutionSource','slug'}
SNAPSHOTS = {'candles','daily_candles','underlying_context'}


class SourceCodec:
    """Schema/identifier dictionaries and at most 15 dependencies per anchor.

    The reconstructed canonical provider payload is hashed, including fields
    the current reducer does not use. Provider byte-stream identity is not
    claimed: v1 also persisted canonical JSON, not original websocket bytes.
    """
    version = 'lossless-json-vector-1'
    max_chain = 15

    def __init__(self, put, get):
        self.put, self.get = put, get
        self.previous = {}

    def vector(self, value, field=None):
        if field in SNAPSHOTS and len(encode(value))>=512:
            return [3,self.put(value)]
        if isinstance(value,dict):
            keys=sorted(value)
            return [0,self.put(keys),[self.vector(value[k],k) for k in keys]]
        if isinstance(value,(list,tuple)):
            return [1,[self.vector(x) for x in value]]
        if isinstance(value,str) and field in IDENTIFIERS and len(value)>=8:
            return [2,self.put(value)]
        return value

    def logical(self, value):
        if not isinstance(value,list):return value
        tag=value[0]
        if tag==0:return dict(zip(self.get(value[1]),(self.logical(x) for x in value[2])))
        if tag==1:return [self.logical(x) for x in value[1]]
        if tag in (2,3):return self.get(value[1])
        raise ValueError('Unknown source vector tag')

    @staticmethod
    def changes(old,new,path=()):
        if type(old) is type(new) and isinstance(new,list) and len(old)==len(new):
            result=[]
            for i,(a,b) in enumerate(zip(old,new)):
                result.extend(SourceCodec.changes(a,b,path+(i,)))
            return result
        # Preserve numeric types and signed zero as well as ordinary values.
        same=type(old) is type(new) and old==new
        if same and isinstance(new,float):same=math.copysign(1,old)==math.copysign(1,new)
        return [] if same else [[list(path),new]]

    @staticmethod
    def patch(old,changes):
        result=copy.deepcopy(old)
        for path,value in changes:
            if not path:result=value;continue
            node=result
            for part in path[:-1]:node=node[part]
            node[path[-1]]=value
        return result

    def pack(self,key,raw_id,received_ms,payload):
        value=self.vector(payload);body=encode(value).encode()
        full=zlib.compress(body,6)
        codec,base,depth,data=0,None,0,full
        previous=self.previous.get(key)
        if previous and previous['depth']<self.max_chain and 0<=received_ms-previous['anchor_ms']<300_000:
            patch=zlib.compress(encode(self.changes(previous['value'],value)).encode(),6)
            compressor=zlib.compressobj(6,zdict=previous['body'][-32768:])
            dictionary=compressor.compress(body)+compressor.flush()
            candidate=min((len(patch),1,patch),(len(dictionary),2,dictionary))
            if candidate[0]<len(full)*.9:
                _,codec,data=candidate;base=previous['id'];depth=previous['depth']+1
        self.previous[key]=dict(id=raw_id,depth=depth,value=value,body=body,
                               anchor_ms=previous['anchor_ms'] if base else received_ms)
        return codec,base,depth,data

    @staticmethod
    def unpack(codec,data,previous=None):
        if codec==0:return decode(data)
        if previous is None:raise ValueError('Missing causal source anchor')
        if codec==1:return SourceCodec.patch(previous,decode(data))
        if codec==2:
            decompressor=zlib.decompressobj(zdict=encode(previous).encode()[-32768:])
            return json.loads(decompressor.decompress(data)+decompressor.flush())
        raise ValueError('Unknown source encoding')


def checkpoint_pack(state,put):
    """Share unchanged calendar-minute history chunks across checkpoints."""
    result={}
    for key,value in state.items():
        if key in ('rolling','salient'):
            groups=[]
            for identity,rows in value:
                buckets=[];bucket=[];minute=None
                for row in rows:
                    at=row.get('timestamp_wall',row.get('timestamp',0))//60000
                    if bucket and at!=minute:
                        buckets.append(put(bucket));bucket=[]
                    minute=at;bucket.append(row)
                if bucket:buckets.append(put(bucket))
                groups.append([identity,buckets])
            result[key]=['minutes',groups]
        else:result[key]=['blob',put(value)]
    return result


def checkpoint_unpack(value,get):
    result={}
    for key,(kind,data) in value.items():
        if kind=='blob':result[key]=get(data)
        elif kind=='minutes':result[key]=[[identity,[row for ref in refs for row in get(ref)]] for identity,refs in data]
        else:raise ValueError('Unknown checkpoint chunk encoding')
    return result


def digest(value):
    return hashlib.sha256(encode(value).encode()).digest()
