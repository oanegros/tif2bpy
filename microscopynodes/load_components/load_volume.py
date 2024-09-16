import bpy
from mathutils import Color
from pathlib import Path
import numpy as np
import math
import itertools
import skimage
import scipy

from .load_generic import *
from ..handle_blender_structs import *
from .. import min_nodes


NR_HIST_BINS = 2**16

def split_axis_to_chunks(length, ch_ix):
    # chunks to max 2048 length, with ch_ix dependent offsets
    offset = 1
    if length > 2048:
        offset = 256 * ch_ix
    length += offset
    n_splits = int((length // 2049)+ 1)
    splits = [length/n_splits * split for split in range(n_splits + 1)]
    splits[-1] = math.ceil(splits[-1]) 
    splits = [math.floor(split) for split in splits]
    while splits[-2] > (length - offset):
        del splits[-1]
    splits[-1] = (length - offset)
    slices = [slice(start, end) for start, end in zip(splits[:-1], splits[1:])]
    return slices

def len_axis(dim, axes_order, shape):
    if dim in axes_order:
        return shape[axes_order.find(dim)]
    return 1

def take_index(imgdata, indices, dim, axes_order):
    if dim in axes_order:
        return np.take(imgdata, indices=indices, axis=axes_order.find(dim))
    return imgdata

def arrays_to_vdb_files(ch_dicts, axes_order, remake, cache_dir):
    # 2048 is maximum grid size for Eevee rendering, so grids are split for multiple
    # Loops over all axes and splits based on length
    # reassembles in negative coordinates, parents all to a parent at (half_x, half_y, bottom) that is then translated to (0,0,0)
    for ch in ch_dicts:
        ch['local_files'] = []
        if ch['volume'] == False and ch['surface'] == False:
            continue
        
        xyz_shape = [len_axis(dim, axes_order, ch['data'].shape) for dim in 'xyz']
        slices = [split_axis_to_chunks(dimshape, ch['ix']) for dimshape in xyz_shape]
        for block in itertools.product(*slices):
            chunk = ch['data']
            for dim, sl in zip('xyz', block): 
                chunk = take_index(chunk, indices = np.arange(sl.start, sl.stop), dim=dim, axes_order=axes_order)
            directory, time_vdbs, time_hists = make_vdb(chunk, block, axes_order, remake, cache_dir, ch['ix'])
            ch['local_files'].append({"directory" : directory, "vdbfiles": time_vdbs, 'histfiles' : time_hists, 'pos':(block[0].start, block[1].start, block[2].start)})
        del ch['data']
    return

def make_vdb(imgdata, block, axes_order, remake, cache_dir, ch):
    # non-lazy functions are allowed on only single time-frames
    import pyopenvdb as vdb
    x_ix, y_ix, z_ix = [sl.start for sl in block]

    # imgdata = imgdata.compute()
    time_vdbs = [] 
    time_hists = []

    identifier3d = f"x{x_ix}y{y_ix}z{z_ix}"
    dirpath = Path(cache_dir)/f"{identifier3d}"
    dirpath.mkdir(exist_ok=True,parents=True)
    for t in range(len_axis('t', axes_order, imgdata.shape)):
        identifier5d = f"{identifier3d}c{ch}t{t:04}"
        frame = take_index(imgdata, t, 't', axes_order)
        frame_axes_order = axes_order.replace('t',"")

        # VDB data is XYZ
        for dim in 'xyz':
            if dim not in axes_order:
                frame = np.expand_dims(frame,axis=0)
                frame_axes_order = dim + frame_axes_order          

       
        vdbfname = dirpath / f"{identifier5d}.vdb"
        histfname = dirpath / f"{identifier5d}_hist.npy"
        time_vdbs.append({"name":str(vdbfname.name)})
        time_hists.append({"name":str(histfname.name)})
        if( not vdbfname.exists() or not histfname.exists()) or remake :
            if vdbfname.exists():
                vdbfname.unlink()
            if histfname.exists():
                histfname.unlink()
            # frame.visualize(filename=f'/Users/oanegros/Documents/screenshots/stranspose-hlg{x_ix}_{y_ix}_{z_ix}.svg', engine='cytoscape')
            # arr = frame.compute()
            log(f"loading chunk {identifier5d}")
            arr = frame.compute()
            arr = np.moveaxis(arr, [frame_axes_order.find('x'),frame_axes_order.find('y'),frame_axes_order.find('z')],[0,1,2]).copy()
            try:
                arr = arr.astype(np.float32) / np.iinfo(imgdata.dtype).max # scale between 0 and 1
            except ValueError:
                arr = arr.astype(np.float32) / ch['max_val']

            # hists could be done better with bincount, but this doesnt work with floats and seems harder to maintain
            histogram = np.histogram(arr, bins=NR_HIST_BINS, range=(0.,1.)) [0]
            histogram[0] = 0
            np.save(histfname, histogram, allow_pickle=False)

            grid = vdb.FloatGrid()
            grid.name = f"data_channel_{ch}"
            
            grid.copyFromArray(arr.astype(np.float32))

            log(f"write vdb {identifier5d}")
            vdb.write(str(vdbfname), grids=[grid])
            # log("")

    return str(dirpath), time_vdbs, time_hists

def get_leading_trailing_zero_float(arr):
    min_val = max(np.argmax(arr > 0)-1, 0) / len(arr)
    max_val = min(len(arr) - (np.argmax(arr[::-1] > 0)-1), len(arr)) / len(arr)
    return min_val, max_val

def draw_histogram(nodes, loc, width, hist):
    histnode =nodes.new(type="ShaderNodeFloatCurve")
    histnode.location = loc
    histmap = histnode.mapping
    histnode.width = width
    histnode.label = 'Histogram (non-interactive)' 
    histnode.name = '[Histogram]'
    histnode.inputs.get('Factor').hide = True
    histnode.inputs.get('Value').hide = True
    histnode.outputs.get('Value').hide = True

    histnorm = hist / np.max(hist)
    if len(histnorm) > 150:
        histnorm = scipy.stats.binned_statistic(np.arange(len(histnorm)), histnorm, bins=150,statistic='sum')[0]
        histnorm /= np.max(histnorm) 
    for ix, val in enumerate(histnorm):
        if ix == 0:
            histmap.curves[0].points[-1].location = ix/len(histnorm), val
            histmap.curves[0].points.new((ix + 0.9)/len(histnorm), val)
        if ix==len(histnorm)-1:
            histmap.curves[0].points[-1].location = ix/len(histnorm), val
        else:
            histmap.curves[0].points.new(ix/len(histnorm), val)
            histmap.curves[0].points.new((ix + 0.9)/len(histnorm), val)
        histmap.curves[0].points[ix].handle_type = 'VECTOR'
    return histnode

def update_shader(mat, ch, replace_hist=True):
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    node_names = [node.name for node in nodes]

    if ('[Histogram]' in node_names and replace_hist) and ch['collection'] is not None:
        histnode= nodes["[Histogram]"]
        draw_histogram(nodes, histnode.location,histnode.width, ch['histnorm'])
        nodes.remove(histnode)

    try:
        ch_load = nodes[f"[channel_load_{ch['identifier']}]"]
        shader_in = nodes['[shader_in]']
        shader_out = nodes['[shader_out]']
    except KeyError as e:
        print(e, " skipping update of shader")
        return

    if '[shaderframe]' not in node_names:
        shaderframe = nodes.new('NodeFrame')
        shaderframe.name = '[shaderframe]'
        shaderframe.use_custom_color = True
        shaderframe.color = (0.2,0.2,0.2)
        shader_in.parent = shaderframe
        shader_out.parent = shaderframe
    else:
        shaderframe = nodes['[shaderframe]']

    ch_load.label = ch['name']
    # removes of other type, if any of current type exist, don't update
    setting, remove = 'absorb', 'emit'
    if ch['emission']:
        setting, remove = 'emit', 'absorb'

    for node in nodes:
        if remove in node.name:
            nodes.remove(node)
        elif setting in node.name:
            return
    
    if ch['emission']:
        emit = nodes.new(type='ShaderNodeEmission')
        emit.name = '[emit]'
        emit.location = (250,0)
        links.new(shader_in.outputs[0], emit.inputs.get('Color'))
        links.new(emit.outputs[0], shader_out.inputs[0])
        emit.parent=shaderframe
        return
    
    scale = nodes.new(type='ShaderNodeVectorMath')
    scale.name = "scale [absorb]"
    scale.location = (-150,-100)
    scale.operation = "SCALE"
    links.new(shader_in.outputs[0], scale.inputs.get("Vector"))
    scale.inputs.get('Scale').default_value = 1
    scale.parent=shaderframe
    
    adsorb = nodes.new(type='ShaderNodeVolumeAbsorption')
    adsorb.name = 'absorb [absorb]'
    adsorb.location = (50,-100)
    links.new(shader_in.outputs[0], adsorb.inputs.get('Color'))
    links.new(scale.outputs[0], adsorb.inputs.get('Density'))
    scatter = nodes.new(type='ShaderNodeVolumeScatter')
    scatter.name = 'scatter absorb'
    scatter.location = (250,-200)
    links.new(shader_in.outputs[0], scatter.inputs.get('Color'))
    links.new(scale.outputs[0], scatter.inputs.get('Density'))
    scatter.parent=shaderframe

    add = nodes.new(type='ShaderNodeAddShader')
    add.name = 'add [absorb]'
    add.location = (450, -100)
    links.new(adsorb.outputs[0], add.inputs[0])
    links.new(scatter.outputs[0], add.inputs[1])
    links.new(add.outputs[0], shader_out.inputs[0])
    add.parent=shaderframe
    return


def volume_materials(obj, ch_dicts):
    if obj is None:
        return
    mod = get_min_gn(obj)
    all_ch_present = len([node.name for node in get_min_gn(obj).node_group.nodes if f"channel_load" in node.name])

    for vol_ix, ch in enumerate(ch_dicts):
        if ch['collection'] is None or ch_present(obj, ch['identifier']):
            print('trying to skip')
            ch['material'] = None
            continue

        mat = bpy.data.materials.new(f"{ch['name']} volume")
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        if nodes.get("Principled BSDF") is not None:
            nodes.remove(nodes.get("Principled BSDF"))
        if nodes.get("Principled Volume") is not None:
            nodes.remove(nodes.get("Principled Volume"))

        node_attr = nodes.new(type='ShaderNodeAttribute')
        node_attr.location = (-1400, 0)
        node_attr.name = f"[channel_load_{ch['identifier']}]"
        node_attr.attribute_name = f'data_channel_{ch["ix"]}'
        node_attr.label = ch['name']

        normnode = nodes.new(type="ShaderNodeMapRange")
        normnode.location = (-1200, 0)
        normnode.label = "Normalize data"
        normnode.inputs[1].default_value = ch['min_val']       
        normnode.inputs[2].default_value = ch['max_val']    
        links.new(node_attr.outputs.get("Fac"), normnode.inputs[0])  
        normnode.hide = True

        ramp_node = nodes.new(type="ShaderNodeValToRGB")
        ramp_node.location = (-1000, 0)
        ramp_node.width = 1000
        ramp_node.color_ramp.elements[0].position = ch['threshold']
        ramp_node.color_ramp.elements[1].position = 1
        links.new(normnode.outputs.get('Result'), ramp_node.inputs.get("Fac"))  

        draw_histogram(nodes, (-1000, 300), 1000, ch['histnorm'])

        color = get_cmap('default_ch')[all_ch_present % len(get_cmap('default_ch'))]
        all_ch_present += 1
        ramp_node.color_ramp.elements[1].color = (color[0],color[1],color[2],color[3])  

        shader_in = nodes.new('NodeReroute')
        shader_in.name = f"[shader_in]"
        shader_in.location = (-200, 0)
        links.new(ramp_node.outputs[0], shader_in.inputs[0])
        
        shader_out = nodes.new('NodeReroute')
        shader_out.location = (600, 0)
        shader_out.name = f"[shader_out]"
        
        update_shader(mat, ch, replace_hist=False)
        
        if nodes.get("Material Output") is None:
            outnode = nodes.new(type='ShaderNodeOutputMaterial')
            outnode.name = 'Material Output'
        links.new(shader_out.outputs[0], nodes.get("Material Output").inputs.get('Volume'))
        nodes.get("Material Output").location = (700,00)
        ch['material'] = mat
    return 


def load_volume(ch_dicts, scale, cache_coll, base_coll, vol_obj=None):
    # consider checking whether all channels are present in vdb for remaking?
    log("loading volumes in Blender")
    collection_activate(*cache_coll)
    vol_collection, vol_lcoll = make_subcollection('volumes')
    volumes = []
    # print(ch_dicts)

    bpy.types.Scene.files: CollectionProperty(
        type=bpy.types.OperatorFileListElement,
        options={'HIDDEN', 'SKIP_SAVE'},
    )

    # (re)load vdb data channels
    vol_ch = [ch for ch in ch_dicts if ch['volume'] or ch['surface']]
    if vol_obj is not None:
        clear_updating_collections(vol_obj, ch_dicts, 'volume')
    
    for ch in vol_ch:
        collection_activate(vol_collection, vol_lcoll)
        activate_or_make_channel_collection(ch, "volume")
        histtotal = np.zeros(NR_HIST_BINS)
        for chunk in ch['local_files']:
            already_loaded = list(ch['collection'].all_objects)
            bpy.ops.object.volume_import(filepath=chunk['vdbfiles'][0]['name'],directory=chunk['directory'], files=chunk['vdbfiles'], align='WORLD', location=(0, 0, 0))

            for vol in ch['collection'].all_objects:
                if vol not in already_loaded:   
                    pos = chunk['pos']
                    strpos = f"{pos[0]}{pos[1]}{pos[2]}"
                
                    vol.scale = scale
                    vol.data.frame_offset = -1
                    vol.data.frame_start = 0
                    vol.data.render.clipping = 1/ (2**17)
                    
                    vol.location = tuple(np.array(chunk['pos']) *scale)                    
            for hist in chunk['histfiles']:
                histtotal += np.load(Path(chunk['directory'])/hist['name'], allow_pickle=False)
        
        
        ch['min_val'] = 0
        ch['max_val'] = 1
        ch['histnorm'] = np.zeros(NR_HIST_BINS)
        print([(k,v) for k, v in ch.items()])
        if 'threshold' in ch:
            print(f'hye found threshold {ch["threshold"]}')
        if 'threshold' not in ch: # crude way of setting metadata TODO rework to handle all omero data
            ch['threshold'] = 0.5
        if np.sum(histtotal)> 0:
            ch['min_val'],ch['max_val'] = get_leading_trailing_zero_float(histtotal)
            ch['histnorm'] = histtotal[int(ch['min_val'] * NR_HIST_BINS): int(ch['max_val'] * NR_HIST_BINS)]
            if ch['threshold'] == 0.5:
                ch['threshold'] = skimage.filters.threshold_isodata(hist=ch['histnorm'] )/len(ch['histnorm'] )  

    collection_activate(*base_coll)
    
    if len(vol_ch) > 0:
        if vol_obj is None:
            vol_obj = init_holder('volume')

    # only generate new materials for new channels, appends them as ch_dict[ch]['material']
    if vol_obj is not None:
        volume_materials(vol_obj, ch_dicts)
        for ch in ch_dicts:
            if ch['material'] is not None:
                vol_obj.data.materials.append(ch['material'])

        update_holder(vol_obj, ch_dicts, 'volume')
    log("")
    
    return vol_obj
