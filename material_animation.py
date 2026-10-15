"""Verified native ACDB v7 material controller transfers (no shader edits).

ANDCHIMERA's HEART_BEAT is a standalone resource with three time-driven curves
and an enabled bin. Its only resource links are the three inAlpha1 material
targets. Preserve every curve, timing, loop and activation byte; retarget only
the parsed/fingerprinted handles. Other controller layouts fail closed.
"""
import hashlib
import struct
from .native import Record

HEART_BEAT_SHA='4448f33a9594f28b4ccd6714ce4d4558a39bddd1e3bcc1546ecd5b21264d68b1'
assert len(HEART_BEAT_SHA)==64
TARGETS=(587,639,691)


def heartbeat_target(record):
    d=record.payload
    if record.kind!=2177 or record.external or len(d)!=715 or d[:16]!=struct.pack('<I',1)+b'ACDB____'+struct.pack('<I',7):return None
    ids=[];normalized=bytearray(d)
    for at in TARGETS:
        if (d[at-12:at-4]!=b'inAlpha1' or struct.unpack_from('<I',d,at-16)[0]!=8 or
            struct.unpack_from('<I',d,at-4)[0]!=1 or struct.unpack_from('<I',d,at)[0]!=2133):return None
        ids.append(struct.unpack_from('<I',d,at+4)[0])
        struct.pack_into('<I',normalized,at+4,0xF6F2)
    if len(set(ids))!=1 or not ids[0] or hashlib.sha256(normalized).hexdigest()!=HEART_BEAT_SHA:return None
    bin_at=d.index(b'HEART_BEAT')+len(b'HEART_BEAT')
    if d[bin_at]!=1:raise ValueError('Native HEART_BEAT bin is not enabled')
    return ids[0]


def transfer_controls(source,destination,old_material,new_material,allocate):
    """Transfer only controllers proven to target this native material."""
    copies=[]
    needle=struct.pack('<II',2133,old_material)
    for rec in source.container.records:
        if rec.kind!=2177:continue
        target=heartbeat_target(rec)
        if target!=old_material:
            if needle in rec.payload:
                raise ValueError(f'Unsupported native material animation controller {rec.asset:X}; its layout must be verified before transfer')
            continue
        payload=bytearray(rec.payload)
        for at in TARGETS:struct.pack_into('<I',payload,at+4,new_material)
        asset=allocate(2177,rec.asset,hashlib.sha256(payload).digest())
        clone=Record(len(destination.container.records),2177,asset,rec.prefix,bytes(payload))
        assert heartbeat_target(clone)==new_material
        destination.container.records.append(clone)
        copies.append(dict(controller=f'{asset:X}',original=f'{rec.asset:X}',bin='HEART_BEAT',parameter='inAlpha1',tracks=3))
    return copies
