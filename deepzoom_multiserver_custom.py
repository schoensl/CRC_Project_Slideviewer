#!/usr/bin/env python
#
# deepzoom_multiserver - Example web application for viewing multiple slides
#
# Copyright (c) 2010-2015 Carnegie Mellon University
# Copyright (c) 2021-2023 Benjamin Gilbert
#
# This library is free software; you can redistribute it and/or modify it
# under the terms of version 2.1 of the GNU Lesser General Public License
# as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public
# License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this library; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

from argparse import ArgumentParser
import base64
from collections import OrderedDict
from io import BytesIO
import os
from threading import Lock
import zlib
import pandas as pd
from urllib.parse import unquote

from PIL import ImageCms
# from PIL import _imagingcms
from flask import Flask, abort, make_response, render_template, url_for

# if os.name == 'nt':
#     _dll_path = os.getenv('OPENSLIDE_PATH')
#     if _dll_path is not None:
#         with os.add_dll_directory(_dll_path):
#             import openslide
#     else:
#         import openslide
# else:
#     import openslide

import openslide
from openslide import OpenSlide, OpenSlideCache, OpenSlideError, OpenSlideVersionError
from openslide.deepzoom import DeepZoomGenerator

# Optimized sRGB v2 profile, CC0-1.0 license
# https://github.com/saucecontrol/Compact-ICC-Profiles/blob/bdd84663/profiles/sRGB-v2-micro.icc
# ImageCms.createProfile() generates a v4 profile and Firefox has problems
# with those: https://littlecms.com/blog/2020/09/09/browser-check/
#SRGB_PROFILE_BYTES = zlib.decompress(
#     base64.b64decode(
#         'eNpjYGA8kZOcW8wkwMCQm1dSFOTupBARGaXA/oiBmUGEgZOBj0E2Mbm4wDfYLYQBCIoT'
#         'y4uTS4pyGFDAt2sMjCD6sm5GYl7K3IkMtg4NG2wdSnQa5y1V6mPADzhTUouTgfQHII5P'
#         'LigqYWBg5AGyecpLCkBsCSBbpAjoKCBbB8ROh7AdQOwkCDsErCYkyBnIzgCyE9KR2ElI'
#         'bKhdIMBaCvQsskNKUitKQLSzswEDKAwgop9DwH5jFDuJEMtfwMBg8YmBgbkfIZY0jYFh'
#         'eycDg8QthJgKUB1/KwPDtiPJpUVlUGu0gLiG4QfjHKZS5maWk2x+HEJcEjxJfF8Ez4t8'
#         'k8iS0VNwVlmjmaVXZ/zacrP9NbdwX7OQshjxFNmcttKwut4OnUlmc1Yv79l0e9/MU8ev'
#         'pz4p//jz/38AR4Nk5Q=='
#     )
# )
#SRGB_PROFILE = ImageCms.getOpenProfile(BytesIO(#SRGB_PROFILE_BYTES))


def create_app(config=None, slide_selection="", annotations_df=""):#, config_file=None):
    # Create and configure app
    global annotations
    annotations = annotations_df
    app = Flask(__name__)
    app.config.from_mapping(
        SLIDE_DIR='.',
        SLIDE_CACHE_SIZE=32,
        SLIDE_TILE_CACHE_MB=512,
        DEEPZOOM_FORMAT='jpeg',
        DEEPZOOM_TILE_SIZE=254,
        DEEPZOOM_OVERLAP=1,
        DEEPZOOM_LIMIT_BOUNDS=True,
        DEEPZOOM_TILE_QUALITY=75,
        DEEPZOOM_COLOR_MODE='default'
    )

    app.config.from_mapping(config)

    # Set up cache
    app.basedir = os.path.abspath(app.config['SLIDE_DIR'])

    
    config_map = {
        'DEEPZOOM_TILE_SIZE': 'tile_size',
        'DEEPZOOM_OVERLAP': 'overlap',
        'DEEPZOOM_LIMIT_BOUNDS': 'limit_bounds',
    }
    opts = {v: app.config[k] for k, v in config_map.items()}
    app.cache = _SlideCache(
        app.config['SLIDE_CACHE_SIZE'],
        app.config['SLIDE_TILE_CACHE_MB'],
        opts,
        app.config['DEEPZOOM_COLOR_MODE'],
    )

    # Helper functions
    def get_slide(path):
        path = unquote(path)
        path = os.path.abspath(os.path.join(app.basedir, path))
        if not path.startswith(app.basedir + os.path.sep):
            # Directory traversal
            abort(404)
        if not os.path.exists(path):
            abort(404)
        try:
            slide = app.cache.get(path)
            slide.filename = os.path.basename(path)
            slide.slide_name = annotations.loc[annotations["slide"] == slide.filename.rsplit(".", 1)[0]].values[0][0]
            slide.slide_label = annotations.loc[annotations["slide"] == slide.filename.rsplit(".", 1)[0]].values[0][1]
            if "fulltext_diagnosis" in annotations.columns:
                slide.fulltext_diagnosis = annotations.loc[annotations["slide"] == slide.filename.rsplit(".", 1)[0]].values[0][2]
            else:
                slide.fulltext_diagnosis = ""
            slide.slide_path = path
            return slide
        except OpenSlideError:
            abort(404)

    # Set up routes
    @app.route('/')
    def index():
        root_dir = _Directory(app.basedir, slide_selection=slide_selection)
        label_dict = {"other": "other", "low-grade dysplasia": "LGD", "high-grade dysplasia": "HGD", "adenocarcinoma": "CRC"}
        global directories
        global names
        global labels
        global indices
        directories = []
        names = []
        labels = []
        indices = []
        # CURRENTLY ONLY WORKS WITH 1 DEPTH 
        index = 0
        for entry in root_dir.children:
            if not isinstance(entry, _Directory):
                if entry.url_path:
                    directories.append(entry.url_path)
                    names.append(entry.name)
                    label = annotations.loc[annotations["slide"] == entry.name.rsplit('.', 1)[0]].values[0][1]
                    labels.append(label_dict[label])
                    indices.append(index)
                    index += 1
            if isinstance(entry, _Directory):
                for entry in entry.children:
                    if entry.url_path:
                        directories.append(entry.url_path)
                        names.append(entry.name)
                        label = annotations.loc[annotations["slide"] == entry.name.rsplit('.', 1)[0]].values[0][1]
                        labels.append(label_dict[label])
                        indices.append(index)
                        index += 1
        return render_template('files.html', root_dir=root_dir)


    @app.route('/<path:path>')
    def slide(path):
        path = unquote(path)
        slide = get_slide(path)
        slide_url = url_for('dzi', path=path)
        return render_template(
            'slide-multipane.html',
            slide_url=slide_url,
            slide_filename=slide.filename,
            slide_mpp=slide.mpp, 
            slide_label = slide.slide_label,
            slide_name = slide.slide_name,
            slide_path = slide.slide_path,
            slide_fulltext_diagnosis = slide.fulltext_diagnosis,
            slidelist = list(zip(directories, names, labels, indices)),
            len_directories = len(directories)
        )
    

    @app.route('/<path:path>.dzi')
    def dzi(path):
        path = unquote(path)
        slide = get_slide(path)
        format = app.config['DEEPZOOM_FORMAT']
        resp = make_response(slide.get_dzi(format))
        resp.mimetype = 'application/xml'
        return resp

    @app.route('/<path:path>_files/<int:level>/<int:col>_<int:row>.<format>')
    def tile(path, level, col, row, format):
        path = unquote(path)
        slide = get_slide(path)
        format = format.lower()
        if format != 'jpeg' and format != 'png':
            # Not supported by Deep Zoom
            abort(404)
        try:
            tile = slide.get_tile(level, (col, row))
        except ValueError:
            # Invalid level or coordinates
            abort(404)
        slide.transform(tile)
        buf = BytesIO()
        tile.save(
            buf,
            format,
            quality=app.config['DEEPZOOM_TILE_QUALITY'],
            icc_profile=tile.info.get('icc_profile'),
        )
        resp = make_response(buf.getvalue())
        resp.mimetype = 'image/%s' % format
        return resp
    return app


class _SlideCache:
    def __init__(self, cache_size, tile_cache_mb, dz_opts, color_mode):
        self.cache_size = cache_size
        self.dz_opts = dz_opts
        self.color_mode = color_mode
        self._lock = Lock()
        self._cache = OrderedDict()
        # Share a single tile cache among all slide handles, if supported
        try:
            self._tile_cache = OpenSlideCache(tile_cache_mb * 1024 * 1024)
        except OpenSlideVersionError:
            self._tile_cache = None

    def get(self, path):
        with self._lock:
            if path in self._cache:
                # Move to end of LRU
                slide = self._cache.pop(path)
                self._cache[path] = slide
                return slide

        osr = OpenSlide(path)
        if self._tile_cache is not None:
            osr.set_cache(self._tile_cache)
        slide = DeepZoomGenerator(osr, **self.dz_opts)
        try:
            mpp_x = osr.properties[openslide.PROPERTY_NAME_MPP_X]
            mpp_y = osr.properties[openslide.PROPERTY_NAME_MPP_Y]
            slide.mpp = (float(mpp_x) + float(mpp_y)) / 2
        except (KeyError, ValueError):
            slide.mpp = 0
        slide.transform = self._get_transform(osr)

        with self._lock:
            if path not in self._cache:
                if len(self._cache) == self.cache_size:
                    self._cache.popitem(last=False)
                self._cache[path] = slide
        return slide

    def _get_transform(self, image):
        if image.color_profile is None:
            return lambda img: None
        mode = self.color_mode
        if mode == 'ignore':
            # drop ICC profile from tiles
            return lambda img: img.info.pop('icc_profile')
        elif mode == 'embed':
            # embed ICC profile in tiles
            return lambda img: None
        elif mode == 'default':
            intent = ImageCms.getDefaultIntent(image.color_profile)
        elif mode == 'absolute-colorimetric':
            intent = ImageCms.Intent.ABSOLUTE_COLORIMETRIC
        elif mode == 'relative-colorimetric':
            intent = ImageCms.Intent.RELATIVE_COLORIMETRIC
        elif mode == 'perceptual':
            intent = ImageCms.Intent.PERCEPTUAL
        elif mode == 'saturation':
            intent = ImageCms.Intent.SATURATION
        else:
            raise ValueError(f'Unknown color mode {mode}')
        transform = ImageCms.buildTransform(
            image.color_profile,
            #SRGB_PROFILE,
            'RGB',
            'RGB',
            intent,
            0,
        )

        def xfrm(img):
            ImageCms.applyTransform(img, transform, True)
            # Some browsers assume we intend the display's color space if we
            # don't embed the profile.  Pillow's serialization is larger, so
            # use ours.
            
            # img.info['icc_profile'] = #SRGB_PROFILE_BYTES

        return xfrm


class _Directory:
    def __init__(self, basedir, relpath='', slide_selection=""):
        self.name = os.path.basename(relpath)
        self.children = []
        
        root_list = os.listdir(os.path.join(basedir, relpath))
        for name in sorted(root_list):
            cur_relpath = os.path.join(relpath, name)
            cur_path = os.path.join(basedir, cur_relpath)
            if os.path.isdir(cur_path): # If found subdir -> 1 iteration deeper
                subdir_name = os.path.splitext(os.path.split(cur_path)[1])[0]
                if subdir_name + ".mrxs" in root_list: # .mrxs specific: if subdirname == a slidename -> skip for faster runtime
                    pass
                else:
                    cur_dir = _Directory(basedir, cur_relpath, slide_selection=slide_selection)
                    if cur_dir.children:
                        self.children.append(cur_dir)
            elif OpenSlide.detect_format(cur_path): # If found slide -> append slide
                detected_file = os.path.split(cur_path)[1]
                filename = (os.path.splitext(detected_file)[0])
                if isinstance(slide_selection, pd.DataFrame):                              
                    if (slide_selection["slide"].eq(filename)).any(): # restrict slides to slide_choice
                        self.children.append(_SlideFile(cur_relpath)) 
                elif slide_selection == "":
                    self.children.append(_SlideFile(cur_relpath)) 
                else:
                    print("error")
                    

class _SlideFile:
    def __init__(self, relpath):
        self.name = os.path.basename(relpath)
        self.url_path = relpath
