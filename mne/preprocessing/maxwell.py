# -*- coding: utf-8 -*-
# Authors: Mark Wronkiewicz <wronk.mark@gmail.com>
#          Eric Larson <larson.eric.d@gmail.com>
#          Jussi Nurminen <jnu@iki.fi>


# License: BSD (3-clause)

from functools import partial
from math import factorial
from os import path as op

import numpy as np
from scipy import linalg

from .. import __version__
from ..bem import _check_origin
from ..chpi import quat_to_rot, rot_to_quat
from ..transforms import (_str_to_frame, _get_trans, Transform, apply_trans,
                          _find_vector_rotation, _cart_to_sph, _get_n_moments,
                          _sph_to_cart_partials, _deg_ord_idx,
                          _sh_complex_to_real, _sh_real_to_complex, _sh_negate)
from ..forward import _concatenate_coils, _prep_meg_channels, _create_meg_coils
from ..surface import _normalize_vectors
from ..io.constants import FIFF
from ..io.proc_history import _read_ctc
from ..io.write import _generate_meas_id, _date_now
from ..io import _loc_to_coil_trans, BaseRaw
from ..io.pick import pick_types, pick_info, pick_channels
from ..utils import verbose, logger, _clean_names, warn, _time_mask
from ..fixes import _get_args, _safe_svd, _get_sph_harm
from ..externals.six import string_types
from ..channels.channels import _get_T1T2_mag_inds


# Note: Elekta uses single precision and some algorithms might use
# truncated versions of constants (e.g., μ0), which could lead to small
# differences between algorithms


@verbose
def maxwell_filter(raw, origin='auto', int_order=8, ext_order=3,
                   calibration=None, cross_talk=None, st_duration=None,
                   st_correlation=0.98, coord_frame='head', destination=None,
                   regularize='in', ignore_ref=False, bad_condition='error',
                   head_pos=None, st_fixed=True, st_only=False, mag_scale=100.,
                   verbose=None):
    u"""Apply Maxwell filter to data using multipole moments.

    .. warning:: Automatic bad channel detection is not currently implemented.
                 It is critical to mark bad channels before running Maxwell
                 filtering, so data should be inspected and marked accordingly
                 prior to running this algorithm.

    .. warning:: Not all features of Elekta MaxFilter™ are currently
                 implemented (see Notes). Maxwell filtering in mne-python
                 is not designed for clinical use.

    Parameters
    ----------
    raw : instance of mne.io.Raw
        Data to be filtered
    origin : array-like, shape (3,) | str
        Origin of internal and external multipolar moment space in meters.
        The default is ``'auto'``, which means a head-digitization-based
        origin fit when ``coord_frame='head'``, and ``(0., 0., 0.)`` when
        ``coord_frame='meg'``.
    int_order : int
        Order of internal component of spherical expansion.
    ext_order : int
        Order of external component of spherical expansion.
    calibration : str | None
        Path to the ``'.dat'`` file with fine calibration coefficients.
        File can have 1D or 3D gradiometer imbalance correction.
        This file is machine/site-specific.
    cross_talk : str | None
        Path to the FIF file with cross-talk correction information.
    st_duration : float | None
        If not None, apply spatiotemporal SSS with specified buffer duration
        (in seconds). Elekta's default is 10.0 seconds in MaxFilter™ v2.2.
        Spatiotemporal SSS acts as implicitly as a high-pass filter where the
        cut-off frequency is 1/st_dur Hz. For this (and other) reasons, longer
        buffers are generally better as long as your system can handle the
        higher memory usage. To ensure that each window is processed
        identically, choose a buffer length that divides evenly into your data.
        Any data at the trailing edge that doesn't fit evenly into a whole
        buffer window will be lumped into the previous buffer.
    st_correlation : float
        Correlation limit between inner and outer subspaces used to reject
        ovwrlapping intersecting inner/outer signals during spatiotemporal SSS.
    coord_frame : str
        The coordinate frame that the ``origin`` is specified in, either
        ``'meg'`` or ``'head'``. For empty-room recordings that do not have
        a head<->meg transform ``info['dev_head_t']``, the MEG coordinate
        frame should be used.
    destination : str | array-like, shape (3,) | None
        The destination location for the head. Can be ``None``, which
        will not change the head position, or a string path to a FIF file
        containing a MEG device<->head transformation, or a 3-element array
        giving the coordinates to translate to (with no rotations).
        For example, ``destination=(0, 0, 0.04)`` would translate the bases
        as ``--trans default`` would in MaxFilter™ (i.e., to the default
        head location).
    regularize : str | None
        Basis regularization type, must be "in" or None.
        "in" is the same algorithm as the "-regularize in" option in
        MaxFilter™.
    ignore_ref : bool
        If True, do not include reference channels in compensation. This
        option should be True for KIT files, since Maxwell filtering
        with reference channels is not currently supported.
    bad_condition : str
        How to deal with ill-conditioned SSS matrices. Can be "error"
        (default), "warning", or "ignore".
    head_pos : array | None
        If array, movement compensation will be performed.
        The array should be of shape (N, 10), holding the position
        parameters as returned by e.g. `read_head_pos`.

        .. versionadded:: 0.12

    st_fixed : bool
        If True (default), do tSSS using the median head position during the
        ``st_duration`` window. This is the default behavior of MaxFilter
        and has been most extensively tested.

        .. versionadded:: 0.12

    st_only : bool
        If True, only tSSS (temporal) projection of MEG data will be
        performed on the output data. The non-tSSS parameters (e.g.,
        ``int_order``, ``calibration``, ``head_pos``, etc.) will still be
        used to form the SSS bases used to calculate temporal projectors,
        but the ouptut MEG data will *only* have temporal projections
        performed. Noise reduction from SSS basis multiplication,
        cross-talk cancellation, movement compensation, and so forth
        will not be applied to the data. This is useful, for example, when
        evoked movement compensation will be performed with
        :func:`mne.epochs.average_movements`.

        .. versionadded:: 0.12

    mag_scale : float | str
        The magenetometer scale-factor used to bring the magnetometers
        to approximately the same order of magnitude as the gradiometers
        (default 100.), as they have different units (T vs T/m).
        Can be ``'auto'`` to use the reciprocal of the physical distance
        between the gradiometer pickup loops (e.g., 0.0168 m yields
        59.5 for VectorView).

        .. versionadded:: 0.13

    verbose : bool, str, int, or None
        If not None, override default verbose level (see :func:`mne.verbose`
        and :ref:`Logging documentation <tut_logging>` for more).

    Returns
    -------
    raw_sss : instance of mne.io.Raw
        The raw data with Maxwell filtering applied.

    See Also
    --------
    mne.epochs.average_movements
    mne.chpi.read_head_pos

    Notes
    -----
    .. versionadded:: 0.11

    Some of this code was adapted and relicensed (with BSD form) with
    permission from Jussi Nurminen. These algorithms are based on work
    from [1]_ and [2]_.

    Compared to Elekta's MaxFilter™ software, our Maxwell filtering
    algorithm currently provides the following features:

        * Bad channel reconstruction
        * Cross-talk cancellation
        * Fine calibration correction
        * tSSS
        * Coordinate frame translation
        * Regularization of internal components using information theory
        * Raw movement compensation
          (using head positions estimated by MaxFilter)
        * cHPI subtraction (see :func:`mne.chpi.filter_chpi`)

    The following features are not yet implemented:

        * **Not certified for clinical use**
        * Automatic bad channel detection
        * Head position estimation

    Our algorithm has the following enhancements:

        * Double floating point precision
        * Handling of 3D (in addition to 1D) fine calibration files
        * Automated processing of split (-1.fif) and concatenated files
        * Epoch-based movement compensation as described in [1]_ through
          :func:`mne.epochs.average_movements`
        * **Experimental** processing of data from (un-compensated)
          non-Elekta systems

    Use of Maxwell filtering routines with non-Elekta systems is currently
    **experimental**. Worse results for non-Elekta systems are expected due
    to (at least):

        * Missing fine-calibration and cross-talk cancellation data for
          other systems.
        * Processing with reference sensors has not been vetted.
        * Regularization of components may not work well for all systems.
        * Coil integration has not been optimized using Abramowitz/Stegun
          definitions.

    .. note:: Various Maxwell filtering algorithm components are covered by
              patents owned by Elekta Oy, Helsinki, Finland.
              These patents include, but may not be limited to:

                  - US2006031038 (Signal Space Separation)
                  - US6876196 (Head position determination)
                  - WO2005067789 (DC fields)
                  - WO2005078467 (MaxShield)
                  - WO2006114473 (Temporal Signal Space Separation)

              These patents likely preclude the use of Maxwell filtering code
              in commercial applications. Consult a lawyer if necessary.

    References
    ----------
    .. [1] Taulu S. and Kajola M. "Presentation of electromagnetic
           multichannel data: The signal space separation method,"
           Journal of Applied Physics, vol. 97, pp. 124905 1-10, 2005.

           http://lib.tkk.fi/Diss/2008/isbn9789512295654/article2.pdf

    .. [2] Taulu S. and Simola J. "Spatiotemporal signal space separation
           method for rejecting nearby interference in MEG measurements,"
           Physics in Medicine and Biology, vol. 51, pp. 1759-1768, 2006.

           http://lib.tkk.fi/Diss/2008/isbn9789512295654/article3.pdf
    """
    # There are an absurd number of different possible notations for spherical
    # coordinates, which confounds the notation for spherical harmonics.  Here,
    # we purposefully stay away from shorthand notation in both and use
    # explicit terms (like 'azimuth' and 'polar') to avoid confusion.
    # See mathworld.wolfram.com/SphericalHarmonic.html for more discussion.
    # Our code follows the same standard that ``scipy`` uses for ``sph_harm``.

    # triage inputs ASAP to avoid late-thrown errors
    if not isinstance(raw, BaseRaw):
        raise TypeError('raw must be Raw, not %s' % type(raw))
    _check_usable(raw)
    _check_regularize(regularize)
    st_correlation = float(st_correlation)
    if st_correlation <= 0. or st_correlation > 1.:
        raise ValueError('Need 0 < st_correlation <= 1., got %s'
                         % st_correlation)
    if coord_frame not in ('head', 'meg'):
        raise ValueError('coord_frame must be either "head" or "meg", not "%s"'
                         % coord_frame)
    head_frame = True if coord_frame == 'head' else False
    recon_trans = _check_destination(destination, raw.info, head_frame)
    if st_duration is not None:
        st_duration = float(st_duration)
        if not 0. < st_duration <= raw.times[-1] + 1. / raw.info['sfreq']:
            raise ValueError('st_duration (%0.1fs) must be between 0 and the '
                             'duration of the data (%0.1fs).'
                             % (st_duration, raw.times[-1]))
        st_correlation = float(st_correlation)
        st_duration = int(round(st_duration * raw.info['sfreq']))
        if not 0. < st_correlation <= 1:
            raise ValueError('st_correlation must be between 0. and 1.')
    if not isinstance(bad_condition, string_types) or \
            bad_condition not in ['error', 'warning', 'ignore']:
        raise ValueError('bad_condition must be "error", "warning", or '
                         '"ignore", not %s' % bad_condition)
    if raw.info['dev_head_t'] is None and coord_frame == 'head':
        raise RuntimeError('coord_frame cannot be "head" because '
                           'info["dev_head_t"] is None; if this is an '
                           'empty room recording, consider using '
                           'coord_frame="meg"')
    if st_only and st_duration is None:
        raise ValueError('st_duration must not be None if st_only is True')
    head_pos = _check_pos(head_pos, head_frame, raw, st_fixed,
                          raw.info['sfreq'])
    _check_info(raw.info, sss=not st_only, tsss=st_duration is not None,
                calibration=not st_only and calibration is not None,
                ctc=not st_only and cross_talk is not None)

    # Now we can actually get moving

    logger.info('Maxwell filtering raw data')
    add_channels = (head_pos[0] is not None) and not st_only
    raw_sss, pos_picks = _copy_preload_add_channels(
        raw, add_channels=add_channels)
    del raw
    _remove_meg_projs(raw_sss)  # remove MEG projectors, they won't apply now
    info = raw_sss.info
    meg_picks, mag_picks, grad_picks, good_picks, mag_or_fine = \
        _get_mf_picks(info, int_order, ext_order, ignore_ref)

    # Magnetometers are scaled to improve numerical stability
    coil_scale, mag_scale = _get_coil_scale(
        meg_picks, mag_picks, grad_picks, mag_scale, info)

    #
    # Fine calibration processing (load fine cal and overwrite sensor geometry)
    #
    sss_cal = dict()
    if calibration is not None:
        calibration, sss_cal = _update_sensor_geometry(info, calibration,
                                                       ignore_ref)
        mag_or_fine.fill(True)  # all channels now have some mag-type data

    # Determine/check the origin of the expansion
    origin = _check_origin(origin, info, coord_frame, disp=True)
    origin.setflags(write=False)
    n_in, n_out = _get_n_moments([int_order, ext_order])

    #
    # Cross-talk processing
    #
    if cross_talk is not None:
        sss_ctc = _read_ctc(cross_talk)
        ctc_chs = sss_ctc['proj_items_chs']
        meg_ch_names = [info['ch_names'][p] for p in meg_picks]
        # checking for extra space ambiguity in channel names
        # between old and new fif files
        if meg_ch_names[0] not in ctc_chs:
            ctc_chs = _clean_names(ctc_chs, remove_whitespace=True)
        missing = sorted(list(set(meg_ch_names) - set(ctc_chs)))
        if len(missing) != 0:
            raise RuntimeError('Missing MEG channels in cross-talk matrix:\n%s'
                               % missing)
        missing = sorted(list(set(ctc_chs) - set(meg_ch_names)))
        if len(missing) > 0:
            warn('Not all cross-talk channels in raw:\n%s' % missing)
        ctc_picks = pick_channels(ctc_chs,
                                  [info['ch_names'][c]
                                   for c in meg_picks[good_picks]])
        assert len(ctc_picks) == len(good_picks)  # otherwise we errored
        ctc = sss_ctc['decoupler'][ctc_picks][:, ctc_picks]
        # I have no idea why, but MF transposes this for storage..
        sss_ctc['decoupler'] = sss_ctc['decoupler'].T.tocsc()
    else:
        sss_ctc = dict()

    #
    # Translate to destination frame (always use non-fine-cal bases)
    #
    exp = dict(origin=origin, int_order=int_order, ext_order=0)
    all_coils = _prep_mf_coils(info, ignore_ref)
    S_recon = _trans_sss_basis(exp, all_coils, recon_trans, coil_scale)
    exp['ext_order'] = ext_order
    # Reconstruct data from internal space only (Eq. 38), and rescale S_recon
    S_recon /= coil_scale
    if recon_trans is not None:
        # warn if we have translated too far
        diff = 1000 * (info['dev_head_t']['trans'][:3, 3] -
                       recon_trans['trans'][:3, 3])
        dist = np.sqrt(np.sum(_sq(diff)))
        if dist > 25.:
            warn('Head position change is over 25 mm (%s) = %0.1f mm'
                 % (', '.join('%0.1f' % x for x in diff), dist))

    # Reconstruct raw file object with spatiotemporal processed data
    max_st = dict()
    if st_duration is not None:
        max_st.update(job=10, subspcorr=st_correlation,
                      buflen=st_duration / info['sfreq'])
        logger.info('    Processing data using tSSS with st_duration=%s'
                    % max_st['buflen'])
        st_when = 'before' if st_fixed else 'after'  # relative to movecomp
    else:
        # st_duration from here on will act like the chunk size
        st_duration = max(int(round(10. * info['sfreq'])), 1)
        st_correlation = None
        st_when = 'never'
    st_duration = min(len(raw_sss.times), st_duration)
    del st_fixed

    # Generate time points to break up data into equal-length windows
    read_lims = np.arange(0, len(raw_sss.times) + 1, st_duration)
    if len(read_lims) == 1:
        read_lims = np.concatenate([read_lims, [len(raw_sss.times)]])
    if read_lims[-1] != len(raw_sss.times):
        read_lims[-1] = len(raw_sss.times)
        # len_last_buf < st_dur so fold it into the previous buffer
        if st_correlation is not None and len(read_lims) > 2:
            logger.info('    Spatiotemporal window did not fit evenly into '
                        'raw object. The final %0.2f seconds were lumped '
                        'onto the previous window.'
                        % ((read_lims[-1] - read_lims[-2]) / info['sfreq'],))
    assert len(read_lims) >= 2
    assert read_lims[0] == 0 and read_lims[-1] == len(raw_sss.times)

    #
    # Do the heavy lifting
    #

    # Figure out which transforms we need for each tSSS block
    # (and transform pos[1] to times)
    head_pos[1] = raw_sss.time_as_index(head_pos[1], use_rounding=True)
    # Compute the first bit of pos_data for cHPI reporting
    if info['dev_head_t'] is not None and head_pos[0] is not None:
        this_pos_quat = np.concatenate([
            rot_to_quat(info['dev_head_t']['trans'][:3, :3]),
            info['dev_head_t']['trans'][:3, 3],
            np.zeros(3)])
    else:
        this_pos_quat = None
    _get_this_decomp_trans = partial(
        _get_decomp, all_coils=all_coils,
        cal=calibration, regularize=regularize,
        exp=exp, ignore_ref=ignore_ref, coil_scale=coil_scale,
        grad_picks=grad_picks, mag_picks=mag_picks, good_picks=good_picks,
        mag_or_fine=mag_or_fine, bad_condition=bad_condition,
        mag_scale=mag_scale)
    S_decomp, pS_decomp, reg_moments, n_use_in = _get_this_decomp_trans(
        info['dev_head_t'], t=0.)
    reg_moments_0 = reg_moments.copy()
    # Loop through buffer windows of data
    n_sig = int(np.floor(np.log10(max(len(read_lims), 0)))) + 1
    pl = 's' if len(read_lims) != 2 else ''
    logger.info('    Processing %s data chunk%s of (at least) %0.1f sec'
                % (len(read_lims) - 1, pl, st_duration / info['sfreq']))
    for ii, (start, stop) in enumerate(zip(read_lims[:-1], read_lims[1:])):
        rel_times = raw_sss.times[start:stop]
        t_str = '%8.3f - %8.3f sec' % tuple(rel_times[[0, -1]])
        t_str += ('(#%d/%d)'
                  % (ii + 1, len(read_lims) - 1)).rjust(2 * n_sig + 5)

        # Get original data
        orig_data = raw_sss._data[meg_picks[good_picks], start:stop]
        # This could just be np.empty if not st_only, but shouldn't be slow
        # this way so might as well just always take the original data
        out_meg_data = raw_sss._data[meg_picks, start:stop]
        # Apply cross-talk correction
        if cross_talk is not None:
            orig_data = ctc.dot(orig_data)
        out_pos_data = np.empty((len(pos_picks), stop - start))

        # Figure out which positions to use
        t_s_s_q_a = _trans_starts_stops_quats(head_pos, start, stop,
                                              this_pos_quat)
        n_positions = len(t_s_s_q_a[0])

        # Set up post-tSSS or do pre-tSSS
        if st_correlation is not None:
            # If doing tSSS before movecomp...
            resid = orig_data.copy()  # to be safe let's operate on a copy
            if st_when == 'after':
                orig_in_data = np.empty((len(meg_picks), stop - start))
            else:  # 'before'
                avg_trans = t_s_s_q_a[-1]
                if avg_trans is not None:
                    # if doing movecomp
                    S_decomp_st, pS_decomp_st, _, n_use_in_st = \
                        _get_this_decomp_trans(avg_trans, t=rel_times[0])
                else:
                    S_decomp_st, pS_decomp_st = S_decomp, pS_decomp
                    n_use_in_st = n_use_in
                orig_in_data = np.dot(np.dot(S_decomp_st[:, :n_use_in_st],
                                             pS_decomp_st[:n_use_in_st]),
                                      resid)
                resid -= np.dot(np.dot(S_decomp_st[:, n_use_in_st:],
                                       pS_decomp_st[n_use_in_st:]), resid)
                resid -= orig_in_data
                # Here we operate on our actual data
                proc = out_meg_data if st_only else orig_data
                _do_tSSS(proc, orig_in_data, resid, st_correlation,
                         n_positions, t_str)

        if not st_only or st_when == 'after':
            # Do movement compensation on the data
            for trans, rel_start, rel_stop, this_pos_quat in \
                    zip(*t_s_s_q_a[:4]):
                # Recalculate bases if necessary (trans will be None iff the
                # first position in this interval is the same as last of the
                # previous interval)
                if trans is not None:
                    S_decomp, pS_decomp, reg_moments, n_use_in = \
                        _get_this_decomp_trans(trans, t=rel_times[rel_start])

                # Determine multipole moments for this interval
                mm_in = np.dot(pS_decomp[:n_use_in],
                               orig_data[:, rel_start:rel_stop])

                # Our output data
                if not st_only:
                    out_meg_data[:, rel_start:rel_stop] = \
                        np.dot(S_recon.take(reg_moments[:n_use_in], axis=1),
                               mm_in)
                if len(pos_picks) > 0:
                    out_pos_data[:, rel_start:rel_stop] = \
                        this_pos_quat[:, np.newaxis]

                # Transform orig_data to store just the residual
                if st_when == 'after':
                    # Reconstruct data using original location from external
                    # and internal spaces and compute residual
                    rel_resid_data = resid[:, rel_start:rel_stop]
                    orig_in_data[:, rel_start:rel_stop] = \
                        np.dot(S_decomp[:, :n_use_in], mm_in)
                    rel_resid_data -= np.dot(np.dot(S_decomp[:, n_use_in:],
                                                    pS_decomp[n_use_in:]),
                                             rel_resid_data)
                    rel_resid_data -= orig_in_data[:, rel_start:rel_stop]

        # If doing tSSS at the end
        if st_when == 'after':
            _do_tSSS(out_meg_data, orig_in_data, resid, st_correlation,
                     n_positions, t_str)
        elif st_when == 'never' and head_pos[0] is not None:
            pl = 's' if n_positions > 1 else ''
            logger.info('        Used % 2d head position%s for %s'
                        % (n_positions, pl, t_str))
        raw_sss._data[meg_picks, start:stop] = out_meg_data
        raw_sss._data[pos_picks, start:stop] = out_pos_data

    # Update info
    info['dev_head_t'] = recon_trans  # set the reconstruction transform
    _update_sss_info(raw_sss, origin, int_order, ext_order, len(good_picks),
                     coord_frame, sss_ctc, sss_cal, max_st, reg_moments_0,
                     st_only)
    logger.info('[done]')
    return raw_sss


def _get_coil_scale(meg_picks, mag_picks, grad_picks, mag_scale, info):
    """Get the magnetometer scale factor."""
    if isinstance(mag_scale, string_types):
        if mag_scale != 'auto':
            raise ValueError('mag_scale must be a float or "auto", got "%s"'
                             % mag_scale)
        if len(mag_picks) in (0, len(meg_picks)):
            mag_scale = 100.  # only one coil type, doesn't matter
            logger.info('    Setting mag_scale=%0.2f because only one '
                        'coil type is present' % mag_scale)
        else:
            # Find our physical distance between gradiometer pickup loops
            # ("base line")
            coils = _create_meg_coils(pick_info(info, meg_picks)['chs'],
                                      'accurate')
            grad_base = set(coils[pick]['base'] for pick in grad_picks)
            if len(grad_base) != 1 or list(grad_base)[0] <= 0:
                raise RuntimeError('Could not automatically determine '
                                   'mag_scale, could not find one '
                                   'proper gradiometer distance from: %s'
                                   % list(grad_base))
            grad_base = list(grad_base)[0]
            mag_scale = 1. / grad_base
            logger.info('    Setting mag_scale=%0.2f based on gradiometer '
                        'distance %0.2f mm' % (mag_scale, 1000 * grad_base))
    mag_scale = float(mag_scale)
    coil_scale = np.ones((len(meg_picks), 1))
    coil_scale[mag_picks] = mag_scale
    return coil_scale, mag_scale


def _remove_meg_projs(inst):
    """Remove inplace existing MEG projectors (assumes inactive)."""
    meg_picks = pick_types(inst.info, meg=True, exclude=[])
    meg_channels = [inst.ch_names[pi] for pi in meg_picks]
    non_meg_proj = list()
    for proj in inst.info['projs']:
        if not any(c in meg_channels for c in proj['data']['col_names']):
            non_meg_proj.append(proj)
    inst.add_proj(non_meg_proj, remove_existing=True, verbose=False)


def _check_destination(destination, info, head_frame):
    """Triage our reconstruction trans."""
    if destination is None:
        return info['dev_head_t']
    if not head_frame:
        raise RuntimeError('destination can only be set if using the '
                           'head coordinate frame')
    if isinstance(destination, string_types):
        recon_trans = _get_trans(destination, 'meg', 'head')[0]
    elif isinstance(destination, Transform):
        recon_trans = destination
    else:
        destination = np.array(destination, float)
        if destination.shape != (3,):
            raise ValueError('destination must be a 3-element vector, '
                             'str, or None')
        recon_trans = np.eye(4)
        recon_trans[:3, 3] = destination
        recon_trans = Transform('meg', 'head', recon_trans)
    if recon_trans.to_str != 'head' or recon_trans.from_str != 'MEG device':
        raise RuntimeError('Destination transform is not MEG device -> head, '
                           'got %s -> %s' % (recon_trans.from_str,
                                             recon_trans.to_str))
    return recon_trans


def _prep_mf_coils(info, ignore_ref=True):
    """Get all coil integration information loaded and sorted."""
    coils, comp_coils = _prep_meg_channels(
        info, accurate=True, elekta_defs=True, head_frame=False,
        ignore_ref=ignore_ref, verbose=False)[:2]
    mag_mask = _get_mag_mask(coils)
    if len(comp_coils) > 0:
        meg_picks = pick_types(info, meg=True, ref_meg=False, exclude=[])
        ref_picks = pick_types(info, meg=False, ref_meg=True, exclude=[])
        inserts = np.searchsorted(meg_picks, ref_picks)
        # len(inserts) == len(comp_coils)
        for idx, comp_coil in zip(inserts[::-1], comp_coils[::-1]):
            coils.insert(idx, comp_coil)
        # Now we have:
        # [c['chname'] for c in coils] ==
        # [info['ch_names'][ii]
        #  for ii in pick_types(info, meg=True, ref_meg=True)]

    # Now coils is a sorted list of coils. Time to do some vectorization.
    n_coils = len(coils)
    rmags = np.concatenate([coil['rmag'] for coil in coils])
    cosmags = np.concatenate([coil['cosmag'] for coil in coils])
    ws = np.concatenate([coil['w'] for coil in coils])
    cosmags *= ws[:, np.newaxis]
    del ws
    n_int = np.array([len(coil['rmag']) for coil in coils])
    bins = np.repeat(np.arange(len(n_int)), n_int)
    bd = np.concatenate(([0], np.cumsum(n_int)))
    slice_map = dict((ii, slice(start, stop))
                     for ii, (start, stop) in enumerate(zip(bd[:-1], bd[1:])))
    return rmags, cosmags, bins, n_coils, mag_mask, slice_map


def _trans_starts_stops_quats(pos, start, stop, this_pos_data):
    """Get all trans and limits we need."""
    pos_idx = np.arange(*np.searchsorted(pos[1], [start, stop]))
    used = np.zeros(stop - start, bool)
    trans = list()
    rel_starts = list()
    rel_stops = list()
    quats = list()
    if this_pos_data is None:
        avg_trans = None
    else:
        avg_trans = np.zeros(6)
    for ti in range(-1, len(pos_idx)):
        # first iteration for this block of data
        if ti < 0:
            rel_start = 0
            rel_stop = pos[1][pos_idx[0]] if len(pos_idx) > 0 else stop
            rel_stop = rel_stop - start
            if rel_start == rel_stop:
                continue  # our first pos occurs on first time sample
            # Don't calculate S_decomp here, use the last one
            trans.append(None)  # meaning: use previous
            quats.append(this_pos_data)
        else:
            rel_start = pos[1][pos_idx[ti]] - start
            if ti == len(pos_idx) - 1:
                rel_stop = stop - start
            else:
                rel_stop = pos[1][pos_idx[ti + 1]] - start
            trans.append(pos[0][pos_idx[ti]])
            quats.append(pos[2][pos_idx[ti]])
        assert 0 <= rel_start
        assert rel_start < rel_stop
        assert rel_stop <= stop - start
        assert not used[rel_start:rel_stop].any()
        used[rel_start:rel_stop] = True
        rel_starts.append(rel_start)
        rel_stops.append(rel_stop)
        if this_pos_data is not None:
            avg_trans += quats[-1][:6] * (rel_stop - rel_start)
    assert used.all()
    # Use weighted average for average trans over the window
    if avg_trans is not None:
        avg_trans /= (stop - start)
        avg_trans = np.vstack([
            np.hstack([quat_to_rot(avg_trans[:3]),
                       avg_trans[3:][:, np.newaxis]]),
            [[0., 0., 0., 1.]]])
    return trans, rel_starts, rel_stops, quats, avg_trans


def _do_tSSS(clean_data, orig_in_data, resid, st_correlation,
             n_positions, t_str):
    """Compute and apply SSP-like projection vectors based on min corr."""
    np.asarray_chkfinite(resid)
    t_proj = _overlap_projector(orig_in_data, resid, st_correlation)
    # Apply projector according to Eq. 12 in [2]_
    msg = ('        Projecting %2d intersecting tSSS components '
           'for %s' % (t_proj.shape[1], t_str))
    if n_positions > 1:
        msg += ' (across %2d positions)' % n_positions
    logger.info(msg)
    clean_data -= np.dot(np.dot(clean_data, t_proj), t_proj.T)


def _copy_preload_add_channels(raw, add_channels):
    """Load data for processing and (maybe) add cHPI pos channels."""
    raw = raw.copy()
    if add_channels:
        kinds = [FIFF.FIFFV_QUAT_1, FIFF.FIFFV_QUAT_2, FIFF.FIFFV_QUAT_3,
                 FIFF.FIFFV_QUAT_4, FIFF.FIFFV_QUAT_5, FIFF.FIFFV_QUAT_6,
                 FIFF.FIFFV_HPI_G, FIFF.FIFFV_HPI_ERR, FIFF.FIFFV_HPI_MOV]
        out_shape = (len(raw.ch_names) + len(kinds), len(raw.times))
        out_data = np.zeros(out_shape, np.float64)
        msg = '    Appending head position result channels and '
        if raw.preload:
            logger.info(msg + 'copying original raw data')
            out_data[:len(raw.ch_names)] = raw._data
            raw._data = out_data
        else:
            logger.info(msg + 'loading raw data from disk')
            raw._preload_data(out_data[:len(raw.ch_names)], verbose=False)
            raw._data = out_data
        assert raw.preload is True
        off = len(raw.ch_names)
        chpi_chs = [
            dict(ch_name='CHPI%03d' % (ii + 1), logno=ii + 1,
                 scanno=off + ii + 1, unit_mul=-1, range=1., unit=-1,
                 kind=kinds[ii], coord_frame=FIFF.FIFFV_COORD_UNKNOWN,
                 cal=1e-4, coil_type=FIFF.FWD_COIL_UNKNOWN, loc=np.zeros(12))
            for ii in range(len(kinds))]
        raw.info['chs'].extend(chpi_chs)
        raw.info._update_redundant()
        raw.info._check_consistency()
        assert raw._data.shape == (raw.info['nchan'], len(raw.times))
        # Return the pos picks
        pos_picks = np.arange(len(raw.ch_names) - len(chpi_chs),
                              len(raw.ch_names))
        return raw, pos_picks
    else:
        if not raw.preload:
            logger.info('    Loading raw data from disk')
            raw.load_data(verbose=False)
        else:
            logger.info('    Using loaded raw data')
        return raw, np.array([], int)


def _check_pos(pos, head_frame, raw, st_fixed, sfreq):
    """Check for a valid pos array and transform it to a more usable form."""
    if pos is None:
        return [None, np.array([-1])]
    if not head_frame:
        raise ValueError('positions can only be used if coord_frame="head"')
    if not st_fixed:
        warn('st_fixed=False is untested, use with caution!')
    if not isinstance(pos, np.ndarray):
        raise TypeError('pos must be an ndarray')
    if pos.ndim != 2 or pos.shape[1] != 10:
        raise ValueError('pos must be an array of shape (N, 10)')
    t = pos[:, 0]
    t_off = raw.first_samp / raw.info['sfreq']
    if not np.array_equal(t, np.unique(t)):
        raise ValueError('Time points must unique and in ascending order')
    # We need an extra 1e-3 (1 ms) here because MaxFilter outputs values
    # only out to 3 decimal places
    if not _time_mask(t, tmin=t_off - 1e-3, tmax=None, sfreq=sfreq).all():
        raise ValueError('Head position time points must be greater than '
                         'first sample offset, but found %0.4f < %0.4f'
                         % (t[0], t_off))
    max_dist = np.sqrt(np.sum(pos[:, 4:7] ** 2, axis=1)).max()
    if max_dist > 1.:
        warn('Found a distance greater than 1 m (%0.3g m) from the device '
             'origin, positions may be invalid and Maxwell filtering could '
             'fail' % (max_dist,))
    dev_head_ts = np.zeros((len(t), 4, 4))
    dev_head_ts[:, 3, 3] = 1.
    dev_head_ts[:, :3, 3] = pos[:, 4:7]
    dev_head_ts[:, :3, :3] = quat_to_rot(pos[:, 1:4])
    pos = [dev_head_ts, t - t_off, pos[:, 1:]]
    return pos


def _get_decomp(trans, all_coils, cal, regularize, exp, ignore_ref,
                coil_scale, grad_picks, mag_picks, good_picks, mag_or_fine,
                bad_condition, t, mag_scale):
    """Get a decomposition matrix and pseudoinverse matrices."""
    #
    # Fine calibration processing (point-like magnetometers and calib. coeffs)
    #
    S_decomp = _get_s_decomp(exp, all_coils, trans, coil_scale, cal,
                             ignore_ref, grad_picks, mag_picks, good_picks,
                             mag_scale)

    #
    # Regularization
    #
    S_decomp, pS_decomp, sing, reg_moments, n_use_in = _regularize(
        regularize, exp, S_decomp, mag_or_fine, t=t)

    # Pseudo-inverse of total multipolar moment basis set (Part of Eq. 37)
    cond = sing[0] / sing[-1]
    logger.debug('    Decomposition matrix condition: %0.1f' % cond)
    if bad_condition != 'ignore' and cond >= 1000.:
        msg = 'Matrix is badly conditioned: %0.0f >= 1000' % cond
        if bad_condition == 'error':
            raise RuntimeError(msg)
        else:  # condition == 'warning':
            warn(msg)

    # Build in our data scaling here
    pS_decomp *= coil_scale[good_picks].T
    S_decomp /= coil_scale[good_picks]
    return S_decomp, pS_decomp, reg_moments, n_use_in


def _get_s_decomp(exp, all_coils, trans, coil_scale, cal, ignore_ref,
                  grad_picks, mag_picks, good_picks, mag_scale):
    """Get S_decomp."""
    S_decomp = _trans_sss_basis(exp, all_coils, trans, coil_scale)
    if cal is not None:
        # Compute point-like mags to incorporate gradiometer imbalance
        grad_cals = _sss_basis_point(exp, trans, cal, ignore_ref, mag_scale)
        # Add point like magnetometer data to bases.
        S_decomp[grad_picks, :] += grad_cals
        # Scale magnetometers by calibration coefficient
        S_decomp[mag_picks, :] /= cal['mag_cals']
        # We need to be careful about KIT gradiometers
    S_decomp = S_decomp[good_picks]
    return S_decomp


@verbose
def _regularize(regularize, exp, S_decomp, mag_or_fine, t, verbose=None):
    """Regularize a decomposition matrix."""
    # ALWAYS regularize the out components according to norm, since
    # gradiometer-only setups (e.g., KIT) can have zero first-order
    # components
    int_order, ext_order = exp['int_order'], exp['ext_order']
    n_in, n_out = _get_n_moments([int_order, ext_order])
    t_str = '%8.3f' % t
    if regularize is not None:  # regularize='in'
        logger.info('    Computing regularization')
        in_removes, out_removes = _regularize_in(
            int_order, ext_order, S_decomp, mag_or_fine)
    else:
        in_removes = []
        out_removes = _regularize_out(int_order, ext_order, mag_or_fine)
    reg_in_moments = np.setdiff1d(np.arange(n_in), in_removes)
    reg_out_moments = np.setdiff1d(np.arange(n_in, n_in + n_out),
                                   out_removes)
    n_use_in = len(reg_in_moments)
    n_use_out = len(reg_out_moments)
    reg_moments = np.concatenate((reg_in_moments, reg_out_moments))
    S_decomp = S_decomp.take(reg_moments, axis=1)
    pS_decomp, sing = _col_norm_pinv(S_decomp.copy())
    if regularize is not None or n_use_out != n_out:
        logger.info('        Using %s/%s harmonic components for %s  '
                    '(%s/%s in, %s/%s out)'
                    % (n_use_in + n_use_out, n_in + n_out, t_str,
                       n_use_in, n_in, n_use_out, n_out))
    return S_decomp, pS_decomp, sing, reg_moments, n_use_in


def _get_mf_picks(info, int_order, ext_order, ignore_ref=False):
    """Pick types for Maxwell filtering."""
    # Check for T1/T2 mag types
    mag_inds_T1T2 = _get_T1T2_mag_inds(info)
    if len(mag_inds_T1T2) > 0:
        warn('%d T1/T2 magnetometer channel types found. If using SSS, it is '
             'advised to replace coil types using "fix_mag_coil_types".'
             % len(mag_inds_T1T2))
    # Get indices of channels to use in multipolar moment calculation
    ref = not ignore_ref
    meg_picks = pick_types(info, meg=True, ref_meg=ref, exclude=[])
    meg_info = pick_info(info, meg_picks)
    del info
    good_picks = pick_types(meg_info, meg=True, ref_meg=ref, exclude='bads')
    n_bases = _get_n_moments([int_order, ext_order]).sum()
    if n_bases > len(good_picks):
        raise ValueError('Number of requested bases (%s) exceeds number of '
                         'good sensors (%s)' % (str(n_bases), len(good_picks)))
    recons = [ch for ch in meg_info['bads']]
    if len(recons) > 0:
        logger.info('    Bad MEG channels being reconstructed: %s' % recons)
    else:
        logger.info('    No bad MEG channels')
    ref_meg = False if ignore_ref else 'mag'
    mag_picks = pick_types(meg_info, meg='mag', ref_meg=ref_meg, exclude=[])
    ref_meg = False if ignore_ref else 'grad'
    grad_picks = pick_types(meg_info, meg='grad', ref_meg=ref_meg, exclude=[])
    assert len(mag_picks) + len(grad_picks) == len(meg_info['ch_names'])
    # Determine which are magnetometers for external basis purposes
    mag_or_fine = np.zeros(len(meg_picks), bool)
    mag_or_fine[mag_picks] = True
    # KIT gradiometers are marked as having units T, not T/M (argh)
    # We need a separate variable for this because KIT grads should be
    # treated mostly like magnetometers (e.g., scaled by 100) for reg
    mag_or_fine[np.array([ch['coil_type'] & 0xFFFF == FIFF.FIFFV_COIL_KIT_GRAD
                          for ch in meg_info['chs']], bool)] = False
    msg = ('    Processing %s gradiometers and %s magnetometers'
           % (len(grad_picks), len(mag_picks)))
    n_kit = len(mag_picks) - mag_or_fine.sum()
    if n_kit > 0:
        msg += ' (of which %s are actually KIT gradiometers)' % n_kit
    logger.info(msg)
    return meg_picks, mag_picks, grad_picks, good_picks, mag_or_fine


def _check_regularize(regularize):
    """Ensure regularize is valid."""
    if not (regularize is None or (isinstance(regularize, string_types) and
                                   regularize in ('in',))):
        raise ValueError('regularize must be None or "in"')


def _check_usable(inst):
    """Ensure our data are clean."""
    if inst.proj:
        raise RuntimeError('Projectors cannot be applied to data.')
    current_comp = inst.compensation_grade
    if current_comp not in (0, None):
        raise RuntimeError('Maxwell filter cannot be done on compensated '
                           'channels, but data have been compensated with '
                           'grade %s.' % current_comp)


def _col_norm_pinv(x):
    """Compute the pinv with column-normalization to stabilize calculation.

    Note: will modify/overwrite x.
    """
    norm = np.sqrt(np.sum(x * x, axis=0))
    x /= norm
    u, s, v = linalg.svd(x, full_matrices=False, overwrite_a=True,
                         **check_disable)
    v /= norm
    return np.dot(v.T * 1. / s, u.T), s


def _sq(x):
    """Square quickly."""
    return x * x


def _check_finite(data):
    """Ensure data is finite."""
    if not np.isfinite(data).all():
        raise RuntimeError('data contains non-finite numbers')


def _sph_harm_norm(order, degree):
    """Normalization factor for spherical harmonics."""
    # we could use scipy.special.poch(degree + order + 1, -2 * order)
    # here, but it's slower for our fairly small degree
    norm = np.sqrt((2 * degree + 1.) / (4 * np.pi))
    if order != 0:
        norm *= np.sqrt(factorial(degree - order) /
                        float(factorial(degree + order)))
    return norm


def _concatenate_sph_coils(coils):
    """Concatenate MEG coil parameters for spherical harmoncs."""
    rs = np.concatenate([coil['r0_exey'] for coil in coils])
    wcoils = np.concatenate([coil['w'] for coil in coils])
    ezs = np.concatenate([np.tile(coil['ez'][np.newaxis, :],
                                  (len(coil['rmag']), 1))
                          for coil in coils])
    bins = np.repeat(np.arange(len(coils)),
                     [len(coil['rmag']) for coil in coils])
    return rs, wcoils, ezs, bins


_mu_0 = 4e-7 * np.pi  # magnetic permeability


def _get_mag_mask(coils):
    """Get the coil_scale for Maxwell filtering."""
    return np.array([coil['coil_class'] == FIFF.FWD_COILC_MAG
                     for coil in coils])


def _sss_basis_basic(exp, coils, mag_scale=100., method='standard'):
    """Compute SSS basis using non-optimized (but more readable) algorithms."""
    int_order, ext_order = exp['int_order'], exp['ext_order']
    origin = exp['origin']
    # Compute vector between origin and coil, convert to spherical coords
    if method == 'standard':
        # Get position, normal, weights, and number of integration pts.
        rmags, cosmags, ws, bins = _concatenate_coils(coils)
        rmags -= origin
        # Convert points to spherical coordinates
        rad, az, pol = _cart_to_sph(rmags).T
        cosmags *= ws[:, np.newaxis]
        del rmags, ws
        out_type = np.float64
    else:  # testing equivalence method
        rs, wcoils, ezs, bins = _concatenate_sph_coils(coils)
        rs -= origin
        rad, az, pol = _cart_to_sph(rs).T
        ezs *= wcoils[:, np.newaxis]
        del rs, wcoils
        out_type = np.complex128
    del origin

    # Set up output matrices
    n_in, n_out = _get_n_moments([int_order, ext_order])
    S_tot = np.empty((len(coils), n_in + n_out), out_type)
    S_in = S_tot[:, :n_in]
    S_out = S_tot[:, n_in:]
    coil_scale = np.ones((len(coils), 1))
    coil_scale[_get_mag_mask(coils)] = mag_scale

    # Compute internal/external basis vectors (exclude degree 0; L/RHS Eq. 5)
    for degree in range(1, max(int_order, ext_order) + 1):
        # Only loop over positive orders, negative orders are handled
        # for efficiency within
        for order in range(degree + 1):
            S_in_out = list()
            grads_in_out = list()
            # Same spherical harmonic is used for both internal and external
            sph = _get_sph_harm()(order, degree, az, pol)
            sph_norm = _sph_harm_norm(order, degree)
            # Compute complex gradient for all integration points
            # in spherical coordinates (Eq. 6). The gradient for rad, az, pol
            # is obtained by taking the partial derivative of Eq. 4 w.r.t. each
            # coordinate.
            az_factor = 1j * order * sph / np.sin(np.maximum(pol, 1e-16))
            pol_factor = (-sph_norm * np.sin(pol) * np.exp(1j * order * az) *
                          _alegendre_deriv(order, degree, np.cos(pol)))
            if degree <= int_order:
                S_in_out.append(S_in)
                in_norm = _mu_0 * rad ** -(degree + 2)
                g_rad = in_norm * (-(degree + 1.) * sph)
                g_az = in_norm * az_factor
                g_pol = in_norm * pol_factor
                grads_in_out.append(_sph_to_cart_partials(az, pol,
                                                          g_rad, g_az, g_pol))
            if degree <= ext_order:
                S_in_out.append(S_out)
                out_norm = _mu_0 * rad ** (degree - 1)
                g_rad = out_norm * degree * sph
                g_az = out_norm * az_factor
                g_pol = out_norm * pol_factor
                grads_in_out.append(_sph_to_cart_partials(az, pol,
                                                          g_rad, g_az, g_pol))
            for spc, grads in zip(S_in_out, grads_in_out):
                # We could convert to real at the end, but it's more efficient
                # to do it now
                if method == 'standard':
                    grads_pos_neg = [_sh_complex_to_real(grads, order)]
                    orders_pos_neg = [order]
                    # Deal with the negative orders
                    if order > 0:
                        # it's faster to use the conjugation property for
                        # our normalized spherical harmonics than recalculate
                        grads_pos_neg.append(_sh_complex_to_real(
                            _sh_negate(grads, order), -order))
                        orders_pos_neg.append(-order)
                    for gr, oo in zip(grads_pos_neg, orders_pos_neg):
                        # Gradients dotted w/integration point weighted normals
                        gr = np.einsum('ij,ij->i', gr, cosmags)
                        vals = np.bincount(bins, gr, len(coils))
                        spc[:, _deg_ord_idx(degree, oo)] = -vals
                else:
                    grads = np.einsum('ij,ij->i', grads, ezs)
                    v = (np.bincount(bins, grads.real, len(coils)) +
                         1j * np.bincount(bins, grads.imag, len(coils)))
                    spc[:, _deg_ord_idx(degree, order)] = -v
                    if order > 0:
                        spc[:, _deg_ord_idx(degree, -order)] = \
                            -_sh_negate(v, order)

    # Scale magnetometers
    S_tot *= coil_scale
    if method != 'standard':
        # Eventually we could probably refactor this for 2x mem (and maybe CPU)
        # savings by changing how spc/S_tot is assigned above (real only)
        S_tot = _bases_complex_to_real(S_tot, int_order, ext_order)
    return S_tot


def _sss_basis(exp, all_coils):
    """Compute SSS basis for given conditions.

    Parameters
    ----------
    exp : dict
        Must contain the following keys:

            origin : ndarray, shape (3,)
                Origin of the multipolar moment space in millimeters
            int_order : int
                Order of the internal multipolar moment space
            ext_order : int
                Order of the external multipolar moment space

    coils : list
        List of MEG coils. Each should contain coil information dict specifying
        position, normals, weights, number of integration points and channel
        type. All coil geometry must be in the same coordinate frame
        as ``origin`` (``head`` or ``meg``).

    Returns
    -------
    bases : ndarray, shape (n_coils, n_mult_moments)
        Internal and external basis sets as a single ndarray.

    Notes
    -----
    Does not incorporate magnetometer scaling factor or normalize spaces.

    Adapted from code provided by Jukka Nenonen.
    """
    rmags, cosmags, bins, n_coils = all_coils[:4]
    int_order, ext_order = exp['int_order'], exp['ext_order']
    n_in, n_out = _get_n_moments([int_order, ext_order])
    S_tot = np.empty((n_coils, n_in + n_out), np.float64)

    rmags = rmags - exp['origin']
    S_in = S_tot[:, :n_in]
    S_out = S_tot[:, n_in:]

    # do the heavy lifting
    max_order = max(int_order, ext_order)
    L = _tabular_legendre(rmags, max_order)
    phi = np.arctan2(rmags[:, 1], rmags[:, 0])
    r_n = np.sqrt(np.sum(rmags * rmags, axis=1))
    r_xy = np.sqrt(rmags[:, 0] * rmags[:, 0] + rmags[:, 1] * rmags[:, 1])
    cos_pol = rmags[:, 2] / r_n  # cos(theta); theta 0...pi
    sin_pol = np.sqrt(1. - cos_pol * cos_pol)  # sin(theta)
    z_only = (r_xy <= 1e-16)
    r_xy[z_only] = 1.
    cos_az = rmags[:, 0] / r_xy  # cos(phi)
    cos_az[z_only] = 1.
    sin_az = rmags[:, 1] / r_xy  # sin(phi)
    sin_az[z_only] = 0.
    del rmags
    # Appropriate vector spherical harmonics terms
    #  JNE 2012-02-08: modified alm -> 2*alm, blm -> -2*blm
    r_nn2 = r_n.copy()
    r_nn1 = 1.0 / (r_n * r_n)
    for degree in range(max_order + 1):
        if degree <= ext_order:
            r_nn1 *= r_n  # r^(l-1)
        if degree <= int_order:
            r_nn2 *= r_n  # r^(l+2)

        # mu_0*sqrt((2l+1)/4pi (l-m)!/(l+m)!)
        mult = 2e-7 * np.sqrt((2 * degree + 1) * np.pi)

        if degree > 0:
            idx = _deg_ord_idx(degree, 0)
            # alpha
            if degree <= int_order:
                b_r = mult * (degree + 1) * L[degree][0] / r_nn2
                b_pol = -mult * L[degree][1] / r_nn2
                S_in[:, idx] = _integrate_points(
                    cos_az, sin_az, cos_pol, sin_pol, b_r, 0., b_pol,
                    cosmags, bins, n_coils)
            # beta
            if degree <= ext_order:
                b_r = -mult * degree * L[degree][0] * r_nn1
                b_pol = -mult * L[degree][1] * r_nn1
                S_out[:, idx] = _integrate_points(
                    cos_az, sin_az, cos_pol, sin_pol, b_r, 0., b_pol,
                    cosmags, bins, n_coils)
        for order in range(1, degree + 1):
            ord_phi = order * phi
            sin_order = np.sin(ord_phi)
            cos_order = np.cos(ord_phi)
            mult /= np.sqrt((degree - order + 1) * (degree + order))
            factor = mult * np.sqrt(2)  # equivalence fix (Elekta uses 2.)

            # Real
            idx = _deg_ord_idx(degree, order)
            r_fact = factor * L[degree][order] * cos_order
            az_fact = factor * order * sin_order * L[degree][order]
            pol_fact = -factor * (L[degree][order + 1] -
                                  (degree + order) * (degree - order + 1) *
                                  L[degree][order - 1]) * cos_order
            # alpha
            if degree <= int_order:
                b_r = (degree + 1) * r_fact / r_nn2
                b_az = az_fact / (sin_pol * r_nn2)
                b_az[z_only] = 0.
                b_pol = pol_fact / (2 * r_nn2)
                S_in[:, idx] = _integrate_points(
                    cos_az, sin_az, cos_pol, sin_pol, b_r, b_az, b_pol,
                    cosmags, bins, n_coils)
            # beta
            if degree <= ext_order:
                b_r = -degree * r_fact * r_nn1
                b_az = az_fact * r_nn1 / sin_pol
                b_az[z_only] = 0.
                b_pol = pol_fact * r_nn1 / 2.
                S_out[:, idx] = _integrate_points(
                    cos_az, sin_az, cos_pol, sin_pol, b_r, b_az, b_pol,
                    cosmags, bins, n_coils)

            # Imaginary
            idx = _deg_ord_idx(degree, -order)
            r_fact = factor * L[degree][order] * sin_order
            az_fact = factor * order * cos_order * L[degree][order]
            pol_fact = factor * (L[degree][order + 1] -
                                 (degree + order) * (degree - order + 1) *
                                 L[degree][order - 1]) * sin_order
            # alpha
            if degree <= int_order:
                b_r = -(degree + 1) * r_fact / r_nn2
                b_az = az_fact / (sin_pol * r_nn2)
                b_az[z_only] = 0.
                b_pol = pol_fact / (2 * r_nn2)
                S_in[:, idx] = _integrate_points(
                    cos_az, sin_az, cos_pol, sin_pol, b_r, b_az, b_pol,
                    cosmags, bins, n_coils)
            # beta
            if degree <= ext_order:
                b_r = degree * r_fact * r_nn1
                b_az = az_fact * r_nn1 / sin_pol
                b_az[z_only] = 0.
                b_pol = pol_fact * r_nn1 / 2.
                S_out[:, idx] = _integrate_points(
                    cos_az, sin_az, cos_pol, sin_pol, b_r, b_az, b_pol,
                    cosmags, bins, n_coils)
    return S_tot


def _integrate_points(cos_az, sin_az, cos_pol, sin_pol, b_r, b_az, b_pol,
                      cosmags, bins, n_coils):
    """Integrate points in spherical coords."""
    grads = _sp_to_cart(cos_az, sin_az, cos_pol, sin_pol, b_r, b_az, b_pol).T
    grads = np.einsum('ij,ij->i', grads, cosmags)
    return np.bincount(bins, grads, n_coils)


def _tabular_legendre(r, nind):
    """Compute associated Legendre polynomials."""
    r_n = np.sqrt(np.sum(r * r, axis=1))
    x = r[:, 2] / r_n  # cos(theta)
    L = list()
    for degree in range(nind + 1):
        L.append(np.zeros((degree + 2, len(r))))
    L[0][0] = 1.
    pnn = 1.
    fact = 1.
    sx2 = np.sqrt((1. - x) * (1. + x))
    for degree in range(nind + 1):
        L[degree][degree] = pnn
        pnn *= (-fact * sx2)
        fact += 2.
        if degree < nind:
            L[degree + 1][degree] = x * (2 * degree + 1) * L[degree][degree]
        if degree >= 2:
            for order in range(degree - 1):
                L[degree][order] = (x * (2 * degree - 1) *
                                    L[degree - 1][order] -
                                    (degree + order - 1) *
                                    L[degree - 2][order]) / (degree - order)
    return L


def _sp_to_cart(cos_az, sin_az, cos_pol, sin_pol, b_r, b_az, b_pol):
    """Convert spherical coords to cartesian."""
    return np.array([(sin_pol * cos_az * b_r +
                      cos_pol * cos_az * b_pol - sin_az * b_az),
                     (sin_pol * sin_az * b_r +
                      cos_pol * sin_az * b_pol + cos_az * b_az),
                     cos_pol * b_r - sin_pol * b_pol])


def _get_degrees_orders(order):
    """Get the set of degrees used in our basis functions."""
    degrees = np.zeros(_get_n_moments(order), int)
    orders = np.zeros_like(degrees)
    for degree in range(1, order + 1):
        # Only loop over positive orders, negative orders are handled
        # for efficiency within
        for order in range(degree + 1):
            ii = _deg_ord_idx(degree, order)
            degrees[ii] = degree
            orders[ii] = order
            ii = _deg_ord_idx(degree, -order)
            degrees[ii] = degree
            orders[ii] = -order
    return degrees, orders


def _alegendre_deriv(order, degree, val):
    """Compute the derivative of the associated Legendre polynomial at a value.

    Parameters
    ----------
    order : int
        Order of spherical harmonic. (Usually) corresponds to 'm'.
    degree : int
        Degree of spherical harmonic. (Usually) corresponds to 'l'.
    val : float
        Value to evaluate the derivative at.

    Returns
    -------
    dPlm : float
        Associated Legendre function derivative
    """
    from scipy.special import lpmv
    assert order >= 0
    return (order * val * lpmv(order, degree, val) + (degree + order) *
            (degree - order + 1.) * np.sqrt(1. - val * val) *
            lpmv(order - 1, degree, val)) / (1. - val * val)


def _bases_complex_to_real(complex_tot, int_order, ext_order):
    """Convert complex spherical harmonics to real."""
    n_in, n_out = _get_n_moments([int_order, ext_order])
    complex_in = complex_tot[:, :n_in]
    complex_out = complex_tot[:, n_in:]
    real_tot = np.empty(complex_tot.shape, np.float64)
    real_in = real_tot[:, :n_in]
    real_out = real_tot[:, n_in:]
    for comp, real, exp_order in zip([complex_in, complex_out],
                                     [real_in, real_out],
                                     [int_order, ext_order]):
        for deg in range(1, exp_order + 1):
            for order in range(deg + 1):
                idx_pos = _deg_ord_idx(deg, order)
                idx_neg = _deg_ord_idx(deg, -order)
                real[:, idx_pos] = _sh_complex_to_real(comp[:, idx_pos], order)
                if order != 0:
                    # This extra mult factor baffles me a bit, but it works
                    # in round-trip testing, so we'll keep it :(
                    mult = (-1 if order % 2 == 0 else 1)
                    real[:, idx_neg] = mult * _sh_complex_to_real(
                        comp[:, idx_neg], -order)
    return real_tot


def _bases_real_to_complex(real_tot, int_order, ext_order):
    """Convert real spherical harmonics to complex."""
    n_in, n_out = _get_n_moments([int_order, ext_order])
    real_in = real_tot[:, :n_in]
    real_out = real_tot[:, n_in:]
    comp_tot = np.empty(real_tot.shape, np.complex128)
    comp_in = comp_tot[:, :n_in]
    comp_out = comp_tot[:, n_in:]
    for real, comp, exp_order in zip([real_in, real_out],
                                     [comp_in, comp_out],
                                     [int_order, ext_order]):
        for deg in range(1, exp_order + 1):
            # only loop over positive orders, figure out neg from pos
            for order in range(deg + 1):
                idx_pos = _deg_ord_idx(deg, order)
                idx_neg = _deg_ord_idx(deg, -order)
                this_comp = _sh_real_to_complex([real[:, idx_pos],
                                                 real[:, idx_neg]], order)
                comp[:, idx_pos] = this_comp
                comp[:, idx_neg] = _sh_negate(this_comp, order)
    return comp_tot


def _check_info(info, sss=True, tsss=True, calibration=True, ctc=True):
    """Ensure that Maxwell filtering has not been applied yet."""
    for ent in info.get('proc_history', []):
        for msg, key, doing in (('SSS', 'sss_info', sss),
                                ('tSSS', 'max_st', tsss),
                                ('fine calibration', 'sss_cal', calibration),
                                ('cross-talk cancellation',  'sss_ctc', ctc)):
            if not doing:
                continue
            if len(ent['max_info'][key]) > 0:
                raise RuntimeError('Maxwell filtering %s step has already '
                                   'been applied, cannot reapply' % msg)


def _update_sss_info(raw, origin, int_order, ext_order, nchan, coord_frame,
                     sss_ctc, sss_cal, max_st, reg_moments, st_only):
    """Update info inplace after Maxwell filtering.

    Parameters
    ----------
    raw : instance of mne.io.Raw
        Data to be filtered
    origin : array-like, shape (3,)
        Origin of internal and external multipolar moment space in head coords
        and in millimeters
    int_order : int
        Order of internal component of spherical expansion
    ext_order : int
        Order of external component of spherical expansion
    nchan : int
        Number of sensors
    sss_ctc : dict
        The cross talk information.
    sss_cal : dict
        The calibration information.
    max_st : dict
        The tSSS information.
    reg_moments : ndarray | slice
        The moments that were used.
    st_only : bool
        Whether tSSS only was performed.
    """
    n_in, n_out = _get_n_moments([int_order, ext_order])
    raw.info['maxshield'] = False
    components = np.zeros(n_in + n_out).astype('int32')
    components[reg_moments] = 1
    sss_info_dict = dict(in_order=int_order, out_order=ext_order,
                         nchan=nchan, origin=origin.astype('float32'),
                         job=np.array([2]), nfree=np.sum(components[:n_in]),
                         frame=_str_to_frame[coord_frame],
                         components=components)
    max_info_dict = dict(max_st=max_st)
    if st_only:
        max_info_dict.update(sss_info=dict(), sss_cal=dict(), sss_ctc=dict())
    else:
        max_info_dict.update(sss_info=sss_info_dict, sss_cal=sss_cal,
                             sss_ctc=sss_ctc)
        # Reset 'bads' for any MEG channels since they've been reconstructed
        _reset_meg_bads(raw.info)
    block_id = _generate_meas_id()
    proc_block = dict(max_info=max_info_dict, block_id=block_id,
                      creator='mne-python v%s' % __version__,
                      date=_date_now(), experimentor='')
    raw.info['proc_history'] = [proc_block] + raw.info.get('proc_history', [])


def _reset_meg_bads(info):
    """Reset MEG bads."""
    meg_picks = pick_types(info, meg=True, exclude=[])
    info['bads'] = [bad for bad in info['bads']
                    if info['ch_names'].index(bad) not in meg_picks]


check_disable = dict()  # not available on really old versions of SciPy
if 'check_finite' in _get_args(linalg.svd):
    check_disable['check_finite'] = False


def _orth_overwrite(A):
    """Create a slightly more efficient 'orth'."""
    # adapted from scipy/linalg/decomp_svd.py
    u, s = _safe_svd(A, full_matrices=False, **check_disable)[:2]
    M, N = A.shape
    eps = np.finfo(float).eps
    tol = max(M, N) * np.amax(s) * eps
    num = np.sum(s > tol, dtype=int)
    return u[:, :num]


def _overlap_projector(data_int, data_res, corr):
    """Calculate projector for removal of subspace intersection in tSSS."""
    # corr necessary to deal with noise when finding identical signal
    # directions in the subspace. See the end of the Results section in [2]_

    # Note that the procedure here is an updated version of [2]_ (and used in
    # Elekta's tSSS) that uses residuals instead of internal/external spaces
    # directly. This provides more degrees of freedom when analyzing for
    # intersections between internal and external spaces.

    # Normalize data, then compute orth to get temporal bases. Matrices
    # must have shape (n_samps x effective_rank) when passed into svd
    # computation

    # we use np.linalg.norm instead of sp.linalg.norm here: ~2x faster!
    n = np.linalg.norm(data_int)
    Q_int = linalg.qr(_orth_overwrite((data_int / n).T),
                      overwrite_a=True, mode='economic', **check_disable)[0].T
    n = np.linalg.norm(data_res)
    Q_res = linalg.qr(_orth_overwrite((data_res / n).T),
                      overwrite_a=True, mode='economic', **check_disable)[0]
    assert data_int.shape[1] > 0
    C_mat = np.dot(Q_int, Q_res)
    del Q_int

    # Compute angles between subspace and which bases to keep
    S_intersect, Vh_intersect = linalg.svd(C_mat, overwrite_a=True,
                                           full_matrices=False,
                                           **check_disable)[1:]
    del C_mat
    intersect_mask = (S_intersect >= corr)
    del S_intersect

    # Compute projection operator as (I-LL_T) Eq. 12 in [2]_
    # V_principal should be shape (n_time_pts x n_retained_inds)
    Vh_intersect = Vh_intersect[intersect_mask].T
    V_principal = np.dot(Q_res, Vh_intersect)
    return V_principal


def _read_fine_cal(fine_cal):
    """Read sensor locations and calib. coeffs from fine calibration file."""
    # Read new sensor locations
    cal_chs = list()
    cal_ch_numbers = list()
    with open(fine_cal, 'r') as fid:
        lines = [line for line in fid if line[0] not in '#\n']
        for line in lines:
            # `vals` contains channel number, (x, y, z), x-norm 3-vec, y-norm
            # 3-vec, z-norm 3-vec, and (1 or 3) imbalance terms
            vals = np.fromstring(line, sep=' ').astype(np.float64)

            # Check for correct number of items
            if len(vals) not in [14, 16]:
                raise RuntimeError('Error reading fine calibration file')

            ch_name = 'MEG' + '%04d' % vals[0]  # Zero-pad names to 4 char
            cal_ch_numbers.append(vals[0])

            # Get orientation information for coil transformation
            loc = vals[1:13].copy()  # Get orientation information for 'loc'
            calib_coeff = vals[13:].copy()  # Get imbalance/calibration coeff
            cal_chs.append(dict(ch_name=ch_name,
                                loc=loc, calib_coeff=calib_coeff,
                                coord_frame=FIFF.FIFFV_COORD_DEVICE))
    return cal_chs, cal_ch_numbers


def _update_sensor_geometry(info, fine_cal, ignore_ref):
    """Replace sensor geometry information and reorder cal_chs."""
    from ._fine_cal import read_fine_calibration
    logger.info('    Using fine calibration %s' % op.basename(fine_cal))
    fine_cal = read_fine_calibration(fine_cal)  # filename -> dict
    ch_names = _clean_names(info['ch_names'], remove_whitespace=True)
    info_order = pick_channels(ch_names, fine_cal['ch_names'])
    meg_picks = pick_types(info, meg=True, exclude=[])
    if len(set(info_order) - set(meg_picks)) != 0:
        # this should never happen
        raise RuntimeError('Found channels in cal file that are not marked '
                           'as MEG channels in the data file')
    if len(info_order) != len(meg_picks):
        raise RuntimeError(
            'Not all MEG channels found in fine calibration file, missing:\n%s'
            % sorted(list(set(ch_names[pick] for pick in meg_picks) -
                          set(fine_cal['ch_names']))))
    rev_order = np.argsort(info_order)
    rev_grad = rev_order[np.in1d(meg_picks,
                                 pick_types(info, meg='grad', exclude=()))]
    rev_mag = rev_order[np.in1d(meg_picks,
                                pick_types(info, meg='mag', exclude=()))]

    # Determine gradiometer imbalances and magnetometer calibrations
    grad_imbalances = np.array([fine_cal['imb_cals'][ri] for ri in rev_grad]).T
    if grad_imbalances.shape[0] not in [1, 3]:
        raise ValueError('Must have 1 (x) or 3 (x, y, z) point-like ' +
                         'magnetometers. Currently have %i' %
                         grad_imbalances.shape[0])
    mag_cals = np.array([fine_cal['imb_cals'][ri] for ri in rev_mag])
    del rev_order, rev_grad, rev_mag
    # Now let's actually construct our point-like adjustment coils for grads
    grad_coilsets = _get_grad_point_coilsets(
        info, n_types=len(grad_imbalances), ignore_ref=ignore_ref)
    calibration = dict(grad_imbalances=grad_imbalances,
                       grad_coilsets=grad_coilsets, mag_cals=mag_cals)

    # Replace sensor locations (and track differences) for fine calibration
    ang_shift = np.zeros((len(fine_cal['ch_names']), 3))
    used = np.zeros(len(info['chs']), bool)
    cal_corrs = list()
    cal_chans = list()
    grad_picks = pick_types(info, meg='grad', exclude=())
    adjust_logged = False
    for ci, info_idx in enumerate(info_order):
        assert ch_names[info_idx] == fine_cal['ch_names'][ci]
        assert not used[info_idx]
        used[info_idx] = True
        info_ch = info['chs'][info_idx]
        ch_num = int(fine_cal['ch_names'][ci].lstrip('MEG').lstrip('0'))
        cal_chans.append([ch_num, info_ch['coil_type']])

        # Some .dat files might only rotate EZ, so we must check first that
        # EX and EY are orthogonal to EZ. If not, we find the rotation between
        # the original and fine-cal ez, and rotate EX and EY accordingly:
        ch_coil_rot = _loc_to_coil_trans(info_ch['loc'])[:3, :3]
        cal_loc = fine_cal['locs'][ci].copy()
        cal_coil_rot = _loc_to_coil_trans(cal_loc)[:3, :3]
        if np.max([np.abs(np.dot(cal_coil_rot[:, ii], cal_coil_rot[:, 2]))
                   for ii in range(2)]) > 1e-6:  # X or Y not orthogonal
            if not adjust_logged:
                logger.info('        Adjusting non-orthogonal EX and EY')
                adjust_logged = True
            # find the rotation matrix that goes from one to the other
            this_trans = _find_vector_rotation(ch_coil_rot[:, 2],
                                               cal_coil_rot[:, 2])
            cal_loc[3:] = np.dot(this_trans, ch_coil_rot).T.ravel()

        # calculate shift angle
        v1 = _loc_to_coil_trans(cal_loc)[:3, :3]
        _normalize_vectors(v1)
        v2 = _loc_to_coil_trans(info_ch['loc'])[:3, :3]
        _normalize_vectors(v2)
        ang_shift[ci] = np.sum(v1 * v2, axis=0)
        if info_idx in grad_picks:
            extra = [1., fine_cal['imb_cals'][ci][0]]
        else:
            extra = [fine_cal['imb_cals'][ci][0], 0.]
        cal_corrs.append(np.concatenate([extra, cal_loc]))
        # Adjust channel normal orientations with those from fine calibration
        # Channel positions are not changed
        info_ch['loc'][3:] = cal_loc[3:]
        assert (info_ch['coord_frame'] == FIFF.FIFFV_COORD_DEVICE)
    assert used[meg_picks].all()
    assert not used[np.setdiff1d(np.arange(len(used)), meg_picks)].any()
    # This gets written to the Info struct
    sss_cal = dict(cal_corrs=np.array(cal_corrs),
                   cal_chans=np.array(cal_chans))

    # Log quantification of sensor changes
    # Deal with numerical precision giving absolute vals slightly more than 1.
    np.clip(ang_shift, -1., 1., ang_shift)
    np.rad2deg(np.arccos(ang_shift), ang_shift)  # Convert to degrees
    logger.info('        Adjusted coil positions by (μ ± σ): '
                '%0.1f° ± %0.1f° (max: %0.1f°)' %
                (np.mean(ang_shift), np.std(ang_shift),
                 np.max(np.abs(ang_shift))))
    return calibration, sss_cal


def _get_grad_point_coilsets(info, n_types, ignore_ref):
    """Get point-type coilsets for gradiometers."""
    grad_coilsets = list()
    grad_info = pick_info(
        info, pick_types(info, meg='grad', exclude=[]), copy=True)
    # Coil_type values for x, y, z point magnetometers
    # Note: 1D correction files only have x-direction corrections
    pt_types = [FIFF.FIFFV_COIL_POINT_MAGNETOMETER_X,
                FIFF.FIFFV_COIL_POINT_MAGNETOMETER_Y,
                FIFF.FIFFV_COIL_POINT_MAGNETOMETER]
    for pt_type in pt_types[:n_types]:
        for ch in grad_info['chs']:
            ch['coil_type'] = pt_type
        grad_coilsets.append(_prep_mf_coils(grad_info, ignore_ref))
    return grad_coilsets


def _sss_basis_point(exp, trans, cal, ignore_ref=False, mag_scale=100.):
    """Compute multipolar moments for point-like mags (in fine cal)."""
    # Loop over all coordinate directions desired and create point mags
    S_tot = 0.
    # These are magnetometers, so use a uniform coil_scale of 100.
    this_cs = np.array([mag_scale], float)
    for imb, coils in zip(cal['grad_imbalances'], cal['grad_coilsets']):
        S_add = _trans_sss_basis(exp, coils, trans, this_cs)
        # Scale spaces by gradiometer imbalance
        S_add *= imb[:, np.newaxis]
        S_tot += S_add

    # Return point-like mag bases
    return S_tot


def _regularize_out(int_order, ext_order, mag_or_fine):
    """Regularize out components based on norm."""
    n_in = _get_n_moments(int_order)
    out_removes = list(np.arange(0 if mag_or_fine.any() else 3) + n_in)
    return list(out_removes)


def _regularize_in(int_order, ext_order, S_decomp, mag_or_fine):
    """Regularize basis set using idealized SNR measure."""
    n_in, n_out = _get_n_moments([int_order, ext_order])

    # The "signal" terms depend only on the inner expansion order
    # (i.e., not sensor geometry or head position / expansion origin)
    a_lm_sq, rho_i = _compute_sphere_activation_in(
        np.arange(int_order + 1))
    degrees, orders = _get_degrees_orders(int_order)
    a_lm_sq = a_lm_sq[degrees]

    I_tots = np.zeros(n_in)  # we might not traverse all, so use np.zeros
    in_keepers = list(range(n_in))
    out_removes = _regularize_out(int_order, ext_order, mag_or_fine)
    out_keepers = list(np.setdiff1d(np.arange(n_in, n_in + n_out),
                                    out_removes))
    remove_order = []
    S_decomp = S_decomp.copy()
    use_norm = np.sqrt(np.sum(S_decomp * S_decomp, axis=0))
    S_decomp /= use_norm
    eigs = np.zeros((n_in, 2))

    # plot = False  # for debugging
    # if plot:
    #     import matplotlib.pyplot as plt
    #     fig, axs = plt.subplots(3, figsize=[6, 12])
    #     plot_ord = np.empty(n_in, int)
    #     plot_ord.fill(-1)
    #     count = 0
    #     # Reorder plot to match MF
    #     for degree in range(1, int_order + 1):
    #         for order in range(0, degree + 1):
    #             assert plot_ord[count] == -1
    #             plot_ord[count] = _deg_ord_idx(degree, order)
    #             count += 1
    #             if order > 0:
    #                 assert plot_ord[count] == -1
    #                 plot_ord[count] = _deg_ord_idx(degree, -order)
    #                 count += 1
    #     assert count == n_in
    #     assert (plot_ord >= 0).all()
    #     assert len(np.unique(plot_ord)) == n_in
    noise_lev = 5e-13  # noise level in T/m
    noise_lev *= noise_lev  # effectively what would happen by earlier multiply
    for ii in range(n_in):
        this_S = S_decomp.take(in_keepers + out_keepers, axis=1)
        u, s, v = linalg.svd(this_S, full_matrices=False, overwrite_a=True,
                             **check_disable)
        del this_S
        eigs[ii] = s[[0, -1]]
        v = v.T[:len(in_keepers)]
        v /= use_norm[in_keepers][:, np.newaxis]
        eta_lm_sq = np.dot(v * 1. / s, u.T)
        del u, s, v
        eta_lm_sq *= eta_lm_sq
        eta_lm_sq = eta_lm_sq.sum(axis=1)
        eta_lm_sq *= noise_lev

        # Mysterious scale factors to match Elekta, likely due to differences
        # in the basis normalizations...
        eta_lm_sq[orders[in_keepers] == 0] *= 2
        eta_lm_sq *= 0.0025
        snr = a_lm_sq[in_keepers] / eta_lm_sq
        I_tots[ii] = 0.5 * np.log2(snr + 1.).sum()
        remove_order.append(in_keepers[np.argmin(snr)])
        in_keepers.pop(in_keepers.index(remove_order[-1]))
        # heuristic to quit if we're past the peak to save cycles
        if ii > 10 and (I_tots[ii - 1:ii + 1] < 0.95 * I_tots.max()).all():
            break
        # if plot and ii == 0:
        #     axs[0].semilogy(snr[plot_ord[in_keepers]], color='k')
    # if plot:
    #     axs[0].set(ylabel='SNR', ylim=[0.1, 500], xlabel='Component')
    #     axs[1].plot(I_tots)
    #     axs[1].set(ylabel='Information', xlabel='Iteration')
    #     axs[2].plot(eigs[:, 0] / eigs[:, 1])
    #     axs[2].set(ylabel='Condition', xlabel='Iteration')
    # Pick the components that give at least 98% of max info
    # This is done because the curves can be quite flat, and we err on the
    # side of including rather than excluding components
    max_info = np.max(I_tots)
    lim_idx = np.where(I_tots >= 0.98 * max_info)[0][0]
    in_removes = remove_order[:lim_idx]
    for ii, ri in enumerate(in_removes):
        logger.debug('            Condition %0.3f/%0.3f = %03.1f, '
                     'Removing in component %s: l=%s, m=%+0.0f'
                     % (tuple(eigs[ii]) + (eigs[ii, 0] / eigs[ii, 1],
                        ri, degrees[ri], orders[ri])))
    logger.debug('        Resulting information: %0.1f bits/sample '
                 '(%0.1f%% of peak %0.1f)'
                 % (I_tots[lim_idx], 100 * I_tots[lim_idx] / max_info,
                    max_info))
    return in_removes, out_removes


def _compute_sphere_activation_in(degrees):
    u"""Compute the "in" power from random currents in a sphere.

    Parameters
    ----------
    degrees : ndarray
        The degrees to evaluate.

    Returns
    -------
    a_power : ndarray
        The a_lm associated for the associated degrees.
    rho_i : float
        The current density.

    Notes
    -----
    See also:

        A 122-channel whole-cortex SQUID system for measuring the brain’s
        magnetic fields. Knuutila et al. IEEE Transactions on Magnetics,
        Vol 29 No 6, Nov 1993.
    """
    r_in = 0.080  # radius of the randomly-activated sphere

    # set the observation point r=r_s, az=el=0, so we can just look at m=0 term
    # compute the resulting current density rho_i

    # This is the "surface" version of the equation:
    # b_r_in = 100e-15  # fixed radial field amplitude at distance r_s = 100 fT
    # r_s = 0.13  # 5 cm from the surface
    # rho_degrees = np.arange(1, 100)
    # in_sum = (rho_degrees * (rho_degrees + 1.) /
    #           ((2. * rho_degrees + 1.)) *
    #           (r_in / r_s) ** (2 * rho_degrees + 2)).sum() * 4. * np.pi
    # rho_i = b_r_in * 1e7 / np.sqrt(in_sum)
    # rho_i = 5.21334885574e-07  # value for r_s = 0.125
    rho_i = 5.91107375632e-07  # deterministic from above, so just store it
    a_power = _sq(rho_i) * (degrees * r_in ** (2 * degrees + 4) /
                            (_sq(2. * degrees + 1.) *
                            (degrees + 1.)))
    return a_power, rho_i


def _trans_sss_basis(exp, all_coils, trans=None, coil_scale=100.):
    """Compute SSS basis (optionally) using a dev<->head trans."""
    if trans is not None:
        if not isinstance(trans, Transform):
            trans = Transform('meg', 'head', trans)
        assert not np.isnan(trans['trans']).any()
        all_coils = (apply_trans(trans, all_coils[0]),
                     apply_trans(trans, all_coils[1], move=False),
                     ) + all_coils[2:]
    if not isinstance(coil_scale, np.ndarray):
        # Scale all magnetometers (with `coil_class` == 1.0) by `mag_scale`
        cs = coil_scale
        coil_scale = np.ones((all_coils[3], 1))
        coil_scale[all_coils[4]] = cs
    S_tot = _sss_basis(exp, all_coils)
    S_tot *= coil_scale
    return S_tot
