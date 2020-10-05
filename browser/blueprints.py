"""Flask blueprint for modular routes."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import base64
import distutils
import distutils.util
import json
import os
import pickle
import re
import timeit
import traceback

from flask import Blueprint
from flask import jsonify
from flask import render_template
from flask import request
from flask import redirect
from flask import current_app
from werkzeug.exceptions import HTTPException
import numpy as np


from helpers import is_trk_file, is_npz_file
from models import db, Project, RawFrame, LabelFrame, RGBFrame, Metadata
from caliban import TrackEdit, ZStackEdit, BaseEdit
from imgutils import pngify, add_outlines


bp = Blueprint('caliban', __name__)  # pylint: disable=C0103


@bp.route('/health')
def health():
    """Returns success if the application is ready."""
    return jsonify({'message': 'success'}), 200


@bp.errorhandler(Exception)
def handle_exception(error):
    """Handle all uncaught exceptions"""
    # pass through HTTP errors
    if isinstance(error, HTTPException):
        return error

    current_app.logger.error('Encountered %s: %s',
                             error.__class__.__name__, error, exc_info=1)

    # now you're handling non-HTTP exceptions only
    return jsonify({'message': str(error)}), 500


@bp.route('/upload_file/<int:project_id>', methods=['GET', 'POST'])
def upload_file(project_id):
    '''Upload .trk/.npz data file to AWS S3 bucket.'''
    start = timeit.default_timer()
    # Use id to grab appropriate TrackEdit/ZStackEdit object from database
    project = Project.get_project_by_id(project_id)

    if not project:
        return jsonify({'error': 'project_id not found'}), 404

    state = load_project_state(project)
    filename = state.file.filename

    # Call function in caliban.py to save data file and send to S3 bucket
    if is_trk_file(filename):
        state.action_save_track()
    elif is_npz_file(filename):
        state.action_save_zstack()

    # add "finished" timestamp and null out state longblob
    Project.finish_project(project)

    current_app.logger.debug('Uploaded file "%s" for project "%s" in %s s.',
                             filename, project_id,
                             timeit.default_timer() - start)

    return redirect('/')


@bp.route('/action/<int:project_id>/<action_type>/<int:frame>', methods=['POST'])
def action(project_id, action_type, frame):
    """
    Make an edit operation to the data file and update the object
    in the database.
    """
    start = timeit.default_timer()
    # obtain 'info' parameter data sent by .js script
    info = {k: json.loads(v) for k, v in request.values.to_dict().items()}

    # TODO: remove frame from request values in front-end
    # Frame is instead tracked by the frame column in the Metadata column
    if 'frame' in info:
        del info['frame']

    try:
        # Get project and current label frame from database
        project = Project.get_project(project_id)
        if not project:
            return jsonify({'error': 'project_id not found'}), 404
        # Get frames from database
        label_frame = LabelFrame.get_frame(project_id, frame)
        raw_frame = RawFrame.get_frame(project_id, frame)
        # Get metadata from project
        metadata = project.metadata_

        # Create Edit object to perform action
        edit = get_edit(metadata, label_frame, raw_frame)

        # Perform edit operation on the data file
        edit.action(action_type, info)
        # Check what changed during the action
        x_changed = edit._x_changed
        y_changed = edit._y_changed
        info_changed = edit.info_changed

    except Exception as e:  # TODO: more error handling to identify problem
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

    tracks = False # Default payload
    if info_changed:
        tracks = metadata.readable_tracks
        # Copy PickleType column so db commits changes
        metadata.cell_info = metadata.cell_info.copy()
        metadata.cell_ids = metadata.cell_ids.copy()

    img_payload = False # Default payload
    if x_changed or y_changed:
        encode = lambda x: base64.encodebytes(x.read()).decode()
        img_payload = {}
        if x_changed:
            raw_png = project.get_raw_png()
            img_payload['raw'] = f'data:image/png;base64,{encode(raw_png)}'
        if y_changed:
            # Copy PickleType column so db commits changes
            label_frame.frame = label_frame.frame.copy()
            label_png = project.get_label_png()
            img_payload['segmented'] = f'data:image/png;base64,{encode(label_png)}'
            img_payload['seg_arr'] = project.get_label_arr()
    db.session.commit()
    current_app.logger.debug('Action "%s" for project "%s" finished in %s s.',
                             action_type, project_id,
                             timeit.default_timer() - start)

    return jsonify({'tracks': tracks, 'imgs': img_payload})


@bp.route('/frame/<int:frame>/<int:project_id>')
def get_frame(frame, project_id):
    """
    Serve modes of frames as pngs. Send pngs and color mappings of
    cells to .js file.
    """
    start = timeit.default_timer()
    # Get project from database
    project = Project.get_project(project_id)
    if not project:
        return jsonify({'error': 'project_id not found'}), 404
    # Get pngs and array from project
    raw_png = project.get_raw_png()
    label_png = project.get_label_png()
    label_arr = project.get_label_arr()
    
    # Create payload
    encode = lambda x: base64.encodebytes(x.read()).decode()
    payload = {
        'raw': f'data:image/png;base64,{encode(raw_png)}',
        'segmented': f'data:image/png;base64,{encode(label_png)}',
        'seg_arr': label_arr
    }

    current_app.logger.debug('Got frame %s of project "%s" in %s s.',
                             frame, project_id, timeit.default_timer() - start)

    return jsonify(payload)


@bp.route('/load/<filename>', methods=['POST'])
def load(filename):
    """
    Initate TrackEdit/ZStackEdit object and load object to database.
    Send specific attributes of the object to the .js file.
    """
    start = timeit.default_timer()
    current_app.logger.info('Loading track at %s', filename)

    folders = re.split('__', filename)
    filename = folders[len(folders) - 1]
    subfolders = folders[2:len(folders) - 1]

    subfolders = '/'.join(subfolders)
    full_path = os.path.join(subfolders, filename)

    input_bucket = folders[0]
    output_bucket = folders[1]

    # arg is 'false' which gets parsed to True if casting to bool
    rgb = request.args.get('rgb', default='false', type=str)
    rgb = bool(distutils.util.strtobool(rgb))

    if not is_trk_file(filename) and not is_npz_file(filename):
        error = {
            'error': 'invalid file extension: {}'.format(
                os.path.splitext(filename)[-1])
        }
        return jsonify(error), 400

    # Initate Project entry in database
    project = Project.create_project(filename, input_bucket, output_bucket, full_path, rgb)
    metadata_start = timeit.default_timer()
    metadata = project.metadata_
    current_app.logger.debug('Got metadata for "%s" in %s s.',
                                 filename, timeit.default_timer() - metadata_start)

    if is_trk_file(filename):
        current_app.logger.debug('Loaded trk file "%s" in %s s.',
                                 filename, timeit.default_timer() - start)
        # Send attributes to .js file
        return jsonify({
            'max_frames': metadata.numFrames,
            'tracks': metadata.readable_tracks,
            'dimensions': (metadata.width, metadata.height),
            'project_id': project.id,
            'screen_scale': metadata.scale_factor
        })

    if is_npz_file(filename):
        current_app.logger.debug('Loaded npz file "%s" in %s s.',
                                 filename, timeit.default_timer() - start)
        # Send attributes to .js file
        return jsonify({
            'max_frames': metadata.numFrames,
            'channel_max': metadata.numChannels,
            'feature_max': metadata.numFeatures,
            'tracks': metadata.readable_tracks,
            'dimensions': (metadata.width, metadata.height),
            'project_id': project.id
        })


@bp.route('/', methods=['GET', 'POST'])
def form():
    """Request HTML landing page to be rendered."""
    return render_template('index.html')


@bp.route('/tool', methods=['GET', 'POST'])
def tool():
    """
    Request HTML caliban tool page to be rendered after user inputs
    filename in the landing page.
    """
    if 'filename' not in request.form:
        return redirect('/')

    filename = request.form['filename']

    current_app.logger.info('%s is filename', filename)

    # TODO: better name template?
    new_filename = 'caliban-input__caliban-output__test__{}'.format(filename)

    # if no options passed (how this route will be for now),
    # still want to pass in default settings
    rgb = request.args.get('rgb', default='false', type=str)
    pixel_only = request.args.get('pixel_only', default='false', type=str)
    label_only = request.args.get('label_only', default='false', type=str)

    # Using distutils to cast string arguments to bools
    settings = {
        'rgb': bool(distutils.util.strtobool(rgb)),
        'pixel_only': bool(distutils.util.strtobool(pixel_only)),
        'label_only': bool(distutils.util.strtobool(label_only))
    }

    if is_trk_file(new_filename):
        filetype = 'track'
        title = 'Tracking Tool'

    elif is_npz_file(new_filename):
        filetype = 'zstack'
        title = 'Z-Stack Tool'

    else:
        # TODO: render an error template instead of JSON.
        error = {
            'error': 'invalid file extension: {}'.format(
                os.path.splitext(filename)[-1])
        }
        return jsonify(error), 400

    return render_template(
        'tool.html',
        filetype=filetype,
        title=title,
        filename=new_filename,
        settings=settings)


@bp.route('/<filename>', methods=['GET', 'POST'])
def shortcut(filename):
    """
    Request HTML caliban tool page to be rendered if user makes a URL
    request to access a specific data file that has been preloaded to the
    input S3 bucket (ex. http://127.0.0.1:5000/test.npz).
    """
    rgb = request.args.get('rgb', default='false', type=str)
    pixel_only = request.args.get('pixel_only', default='false', type=str)
    label_only = request.args.get('label_only', default='false', type=str)

    settings = {
        'rgb': bool(distutils.util.strtobool(rgb)),
        'pixel_only': bool(distutils.util.strtobool(pixel_only)),
        'label_only': bool(distutils.util.strtobool(label_only))
    }

    if is_trk_file(filename):
        filetype = 'track'
        title = 'Tracking Tool'

    elif is_npz_file(filename):
        filetype = 'zstack'
        title = 'Z-Stack Tool'

    else:
        # TODO: render an error template instead of JSON.
        error = {
            'error': 'invalid file extension: {}'.format(
                os.path.splitext(filename)[-1])
        }
        return jsonify(error), 400

    return render_template(
        'tool.html',
        filetype=filetype,
        title=title,
        filename=filename,
        settings=settings)


def get_edit(metadata, label_frame, raw_frame=None):
    """Factory for Edit objects"""
    filename = metadata.filename
    if is_npz_file(filename):
        return ZStackEdit(metadata, label_frame, raw_frame)
    elif is_trk_file(filename):
        # don't use RGB mode with track files
        return TrackEdit(metadata, label_frame, raw_frame)
    return BaseEdit(metadata, label_frame, raw_frame)
