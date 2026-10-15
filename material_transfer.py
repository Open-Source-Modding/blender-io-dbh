"""Native cross-package material copying, isolated IDs and original interfaces."""
import copy
import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .native import Package,Record,VB,Submesh,append_mesh,decode_vertices
from .materials import bindings,texture_info,texture_dependency_offsets,ArchiveTextures
from .texture_bundle import TextureAttachment


def sha(data):return hashlib.sha256(data).hexdigest()


@dataclass
class NativeCarrier:
    sub: object
    vb: object
    positions: list
    y_offset: float
    tree: object = None
    skinned: bool = True


class MaterialTransfer:
    def __init__(self,package,metadata,resources,experimental,fingerprints=None,single_mip=False):
        self.package=package;self.metadata=metadata;self.resources=resources
        self.experimental=experimental;self.fingerprints=fingerprints;self.single_mip=single_mip
        self.packages={};self.materials={};self.copied={};self.canonical={};self.reports=[]
        self.used={r.asset for r in package.container.records}|{a for k,a in package.texture_attachments}
        self.target_hash=sha(package.source_data);self.scope=self.target_hash.encode()

    def load(self,path,expected=''):
        key=str(Path(path).resolve())
        if key not in self.packages:
            raw=Path(path).read_bytes();p=Package(raw)
            self.packages[key]=p
        p=self.packages[key]
        if expected and sha(p.source_data)!=expected:
            raise ValueError(f'Material donor changed since import: {path}. Reimport the donor package.')
        return p

    @staticmethod
    def matches(mat,record):
        table=bindings(record.payload)
        return all(s.binding<len(table) and int(s.asset,16)==table[s.binding].texture
                   for s in mat.dbh_texture_slots if not (mat.dbh_shader_mode=='EYE_EMISSION' and s.binding==5))

    def source(self,mat):
        """Use explicit provenance; recover old blends through their manifests."""
        mid=mat.get('dbh_material_id')
        existing=next((r for r in self.package.container.records if (r.kind,r.asset)==(2133,mid)),None)
        path=mat.get('dbh_material_source','')
        if path:
            if mat.get('dbh_material_source_sha256')==self.target_hash and existing:
                return None
            p=self.load(path,mat.get('dbh_material_source_sha256',''))
            record=next((r for r in p.container.records if (r.kind,r.asset)==(2133,mid)),None)
            if record is None or not self.matches(mat,record):raise ValueError(f'{mat.name}: donor material bindings no longer match')
            return p,dict(source_segs=path,game_index=mat.get('dbh_material_source_index',''))
        if existing is not None and self.matches(mat,existing):return None
        import bpy
        found={}
        for text in bpy.data.texts:
            if not text.name.startswith('DBH Package Metadata'):continue
            try:meta=json.loads(text.as_string())
            except ValueError:continue
            source=meta.get('source_segs','')
            if not source or not Path(source).is_file():continue
            p=self.load(source,meta.get('package_sha256',''))
            r=next((r for r in p.container.records if (r.kind,r.asset)==(2133,mid)),None)
            if r is not None and self.matches(mat,r):found[sha(p.source_data)]=(p,meta)
        if len(found)!=1:
            raise ValueError(f'{mat.name}: cannot uniquely find its original material package. Import the donor SEGS in this blend first.')
        return next(iter(found.values()))

    def allocate(self,kind,old,signature):
        base=0x50000000 if kind==2133 else 0x60000000
        value=base|(int.from_bytes(hashlib.sha256(b'DBH_NATIVE_TRANSFER_V1'+self.scope+struct.pack('<II',kind,old)+signature).digest()[:4],'little')&0xfffffff)
        while value in self.used:value=base|((value+1)&0xfffffff)
        self.used.add(value);return value

    def copy_texture(self,source,asset,archive):
        rec=next((r for r in source.container.records if (r.kind,r.asset)==(2137,asset)),None)
        if rec is None:raise ValueError(f'Transferred material references missing FILETEXT {asset:X}')
        key=(sha(source.source_data),2137,asset)
        if key in self.copied:return self.copied[key]
        prefix=bytearray(rec.prefix);payload=bytearray(rec.payload)
        offsets=texture_dependency_offsets(rec)
        for i,(kind,old,size) in enumerate(texture_info(rec)['dependencies']):
            raw_key=(key[0],kind,old)
            if raw_key not in self.copied:
                item=source.texture_attachments.get((kind,old))
                raw,aux=(item.data,item.auxiliary) if item else archive.raw(kind,old)
                new=self.allocate(kind,old,hashlib.sha256(raw).digest())
                self.package.texture_attachments[kind,new]=TextureAttachment(kind,new,aux,raw)
                self.copied[raw_key]=new
            struct.pack_into('<I',prefix,26+12*i,self.copied[raw_key])
            struct.pack_into('<I',payload,offsets[i]+4,self.copied[raw_key])
        raw=source.members[source.record_members[rec.index]].unpacked if rec.external else None
        signature=hashlib.sha256(bytes(payload)+bytes(prefix)+(raw or b'')).digest()
        new=self.allocate(2137,asset,signature)
        clone=Record(len(self.package.container.records),2137,new,bytes(prefix),bytes(payload))
        texture_dependency_offsets(clone)
        self.package.container.records.append(clone)
        if raw is not None:self.resources[clone.index]=raw
        self.copied[key]=new;return new

    @staticmethod
    def carrier(source,material,offset):
        from .runtime_mesh import catalogs
        high={c.lods[0] for c in catalogs(source)}
        candidates=[]
        for rec in source.container.records:
            if rec.kind!=2130 or rec.index not in source.record_members:continue
            md=source.mesh(rec.index)
            for sub in md.flat():
                if sub.material!=(2133,material) or not sub.count or sub.value(34):continue
                vb=md.vbs[sub.vb];semantics={a[3] for a in vb.attributes()}
                if vb.flag or vb.inline_streams or 0 not in semantics or any(a[0]>=4 for a in vb.attributes()):continue
                skinned={6,7}<=semantics
                if not skinned and (semantics&{6,7,8,9} or 1 not in semantics):continue
                # Static eye shaders retain their own declaration and native
                # NODE attachment. Never invent skin attributes for slot zero.
                candidates.append((not skinned,rec.index not in high,-sub.value(30),rec.index,sub,md,skinned))
        if not candidates:raise ValueError('This native material has no supported ordinary skinned/static vertex interface; virtual cloth or palette-remapped carriers need a matching mesh pipeline')
        _,_,_,_,sub,md,skinned=min(candidates,key=lambda item:item[:4])
        positions=[v['position'] for v in decode_vertices(md,sub)]
        vb=md.vbs[sub.vb]
        streams=[bytearray(s[sub.first_vertex*n:(sub.first_vertex+sub.count)*n]) for s,n in zip(vb.streams,vb.strides)]
        return NativeCarrier(sub,VB(sub.count,0,vb.layout,vb.strides,streams),positions,offset if skinned else 0.,skinned=skinned)

    def material(self,mat):
        pointer=mat.as_pointer()
        if pointer in self.materials:return self.materials[pointer]
        origin=self.source(mat)
        if origin is None:return None
        if not self.experimental:raise ValueError(f'{mat.name}: enable Export Custom Textures to transfer native materials')
        original,meta=origin
        # Apply edited native texture slots/profiles to a PRIVATE donor copy.
        # Blender material IDs, slots and the donor file remain untouched.
        p=copy.deepcopy(original)
        from .texture_export import export_materials
        from .shader_profiles import export_shaders,validate_native_stream
        objects=[SimpleNamespace(data=SimpleNamespace(materials=[mat]))]
        hashes={s.image.as_pointer():self.fingerprints.get(s.image) for s in mat.dbh_texture_slots
                if self.fingerprints and s.image is not None and s.image==s.original_image}
        textures,_=export_materials(p,objects,True,hashes if self.fingerprints else None,self.single_mip)
        shaders,_=export_shaders(p,objects,True)
        if textures or shaders:p=Package(p.rebuild({},resource_data={**textures,**shaders}))
        mid=mat['dbh_material_id'];rec=next(r for r in p.container.records if (r.kind,r.asset)==(2133,mid))
        if not rec.external:raise ValueError('Transferred native material has no supported external shader stream')
        raw=p.members[p.record_members[rec.index]].unpacked
        validate_native_stream(rec,raw)
        from .game_paths import resolve_index
        archive=ArchiveTextures(resolve_index(metadata=meta))
        payload=bytearray(rec.payload);copied={}
        for b in bindings(payload):
            if not b.texture:continue
            copied[b.texture]=self.copy_texture(p,b.texture,archive)
            struct.pack_into('<I',payload,b.offset+4,copied[b.texture])
        asset=self.allocate(2133,mid,hashlib.sha256(bytes(payload)+raw).digest())
        clone=Record(len(self.package.container.records),2133,asset,rec.prefix,bytes(payload))
        validate_native_stream(clone,raw)
        carrier=self.carrier(p,mid,meta.get('y_offset',0.))
        self.package.container.records.append(clone);self.resources[clone.index]=raw
        from .material_animation import transfer_controls
        animations=transfer_controls(p,self.package,mid,asset,self.allocate)
        value=(asset,carrier);self.materials[pointer]=value
        self.reports.append(dict(name=mat.name,source=meta.get('source_segs',''),original=f'{mid:X}',material=f'{asset:X}',
                                 textures={f'{a:X}':f'{b:X}' for a,b in copied.items()},animation_controls=animations,
                                 vertex_pipeline='skinned' if carrier.skinned else 'static'))
        return value

    def append(self,md,sub,vertices,faces,asset,carrier):
        from mathutils.kdtree import KDTree
        if not carrier.skinned:
            target=md.vbs[sub.vb]
            if target.flag or target.inline_streams or {a[3] for a in target.attributes()}&{6,7,8,9}:
                raise ValueError('A native static eye material needs a matching static/bone-attached mesh slot, not a skinned or virtual-cloth draw')
        if carrier.tree is None:
            carrier.tree=KDTree(len(carrier.positions))
            for i,p in enumerate(carrier.positions):carrier.tree.insert(p,i)
            carrier.tree.balance()
        key=id(md),asset
        if key not in self.canonical:
            if len(md.vbs)>=256:raise ValueError('Too many vertex buffers for transferred material')
            self.canonical[key]=len(md.vbs);md.vbs.append(copy.deepcopy(carrier.vb));md.padding.append(b'')
        desc=bytearray(sub.descriptor[:sub.detail_offset])
        struct.pack_into('<I',desc,6,self.canonical[key]);struct.pack_into('<II',desc,10,0,carrier.vb.count)
        struct.pack_into('<I',desc,30,carrier.sub.value(30))
        struct.pack_into('<II',desc,sub.detail_offset-8,2133,asset)
        desc+=carrier.sub.detail_table
        target=Submesh(bytes(desc),sub.suffix)
        delta=self.metadata.get('y_offset',0.)-carrier.y_offset if carrier.skinned else 0.
        values=[]
        for v in vertices:
            x,y,z=v['position'];nearest=carrier.tree.find((x,y+delta,z))[1]
            values.append(dict(v,template_index=nearest))
        return append_mesh(md,target,values,faces)
