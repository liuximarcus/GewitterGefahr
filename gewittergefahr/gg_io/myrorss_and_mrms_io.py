"""IO methods for radar data from MYRORSS or MRMS.

MYRORSS = Multi-year Reanalysis of Remotely Sensed Storms

MRMS = Multi-radar Multi-sensor
"""

import os
import glob
import warnings
import numpy
import pandas
from netCDF4 import Dataset
from gewittergefahr.gg_io import netcdf_io
from gewittergefahr.gg_utils import number_rounding as rounder
from gewittergefahr.gg_utils import time_conversion
from gewittergefahr.gg_utils import time_periods
from gewittergefahr.gg_utils import longitude_conversion as lng_conversion
from gewittergefahr.gg_utils import grids
from gewittergefahr.gg_utils import radar_utils
from gewittergefahr.gg_utils import myrorss_and_mrms_utils
from gewittergefahr.gg_utils import file_system_utils
from gewittergefahr.gg_utils import error_checking

NW_GRID_POINT_LAT_COLUMN_ORIG = 'Latitude'
NW_GRID_POINT_LNG_COLUMN_ORIG = 'Longitude'
LAT_SPACING_COLUMN_ORIG = 'LatGridSpacing'
LNG_SPACING_COLUMN_ORIG = 'LonGridSpacing'
NUM_LAT_COLUMN_ORIG = 'Lat'
NUM_LNG_COLUMN_ORIG = 'Lon'
NUM_PIXELS_COLUMN_ORIG = 'pixel'
HEIGHT_COLUMN_ORIG = 'Height'
UNIX_TIME_COLUMN_ORIG = 'Time'
FIELD_NAME_COLUMN_ORIG = 'TypeName'
SENTINEL_VALUE_COLUMNS_ORIG = ['MissingData', 'RangeFolded']

GRID_ROW_COLUMN = 'grid_row'
GRID_COLUMN_COLUMN = 'grid_column'
NUM_GRID_CELL_COLUMN = 'num_grid_cells'

GRID_ROW_COLUMN_ORIG = 'pixel_x'
GRID_COLUMN_COLUMN_ORIG = 'pixel_y'
NUM_GRID_CELL_COLUMN_ORIG = 'pixel_count'

TIME_FORMAT_SECONDS = '%Y%m%d-%H%M%S'
TIME_FORMAT_MINUTES = '%Y%m%d-%H%M'
TIME_FORMAT_FOR_LOG_MESSAGES = '%Y-%m-%d-%H%M%S'
TIME_FORMAT_SECONDS_REGEX = (
    '[0-9][0-9][0-9][0-9][0-1][0-9][0-3][0-9]-[0-2][0-9][0-5][0-9][0-5][0-9]')

MINUTES_TO_SECONDS = 60
METRES_TO_KM = 1e-3

SENTINEL_TOLERANCE = 10.
LATLNG_MULTIPLE_DEG = 1e-4
DEFAULT_MAX_TIME_OFFSET_FOR_AZ_SHEAR_SEC = 240
DEFAULT_MAX_TIME_OFFSET_FOR_NON_SHEAR_SEC = 180

ZIPPED_FILE_EXTENSION = '.gz'
UNZIPPED_FILE_EXTENSION = '.netcdf'

AZIMUTHAL_SHEAR_FIELD_NAMES = [
    radar_utils.LOW_LEVEL_SHEAR_NAME, radar_utils.MID_LEVEL_SHEAR_NAME]
RADAR_FILE_NAMES_KEY = 'radar_file_name_matrix'
UNIQUE_TIMES_KEY = 'unique_times_unix_sec'
SPC_DATES_AT_UNIQUE_TIMES_KEY = 'spc_dates_at_unique_times_unix_sec'
FIELD_NAME_BY_PAIR_KEY = 'field_name_by_pair'
HEIGHT_BY_PAIR_KEY = 'height_by_pair_m_asl'


def _get_pathless_raw_file_pattern(unix_time_sec):
    """Generates glob pattern for pathless name of raw file.

    This method rounds the time step to the nearest minute and allows the file
    to be either zipped or unzipped.

    The pattern generated by this method is meant for input to `glob.glob`.
    This method is the "pattern" version of _get_pathless_raw_file_name.

    :param unix_time_sec: Valid time.
    :return: pathless_raw_file_pattern: Pathless glob pattern for raw file.
    """

    return '{0:s}*{1:s}*'.format(
        time_conversion.unix_sec_to_string(unix_time_sec, TIME_FORMAT_MINUTES),
        UNZIPPED_FILE_EXTENSION
    )


def _get_pathless_raw_file_name(unix_time_sec, zipped=True):
    """Generates pathless name for raw file.

    :param unix_time_sec: Valid time.
    :param zipped: Boolean flag.  If True, will generate name for zipped file.
        If False, will generate name for unzipped file.
    :return: pathless_raw_file_name: Pathless name for raw file.
    """

    if zipped:
        return '{0:s}{1:s}{2:s}'.format(
            time_conversion.unix_sec_to_string(
                unix_time_sec, TIME_FORMAT_SECONDS),
            UNZIPPED_FILE_EXTENSION,
            ZIPPED_FILE_EXTENSION
        )

    return '{0:s}{1:s}'.format(
        time_conversion.unix_sec_to_string(unix_time_sec, TIME_FORMAT_SECONDS),
        UNZIPPED_FILE_EXTENSION
    )


def _remove_sentinels_from_sparse_grid(
        sparse_grid_table, field_name, sentinel_values):
    """Removes sentinel values from sparse grid.

    :param sparse_grid_table: pandas DataFrame with columns produced by
        `read_data_from_sparse_grid_file`.
    :param field_name: Name of radar field in GewitterGefahr format.
    :param sentinel_values: 1-D numpy array of sentinel values.
    :return: sparse_grid_table: Same as input, except that rows with a sentinel
        value are removed.
    """

    num_rows = len(sparse_grid_table.index)
    sentinel_flags = numpy.full(num_rows, False, dtype=bool)

    for this_sentinel_value in sentinel_values:
        these_sentinel_flags = numpy.isclose(
            sparse_grid_table[field_name].values, this_sentinel_value,
            atol=SENTINEL_TOLERANCE)
        sentinel_flags = numpy.logical_or(sentinel_flags, these_sentinel_flags)

    sentinel_indices = numpy.where(sentinel_flags)[0]
    return sparse_grid_table.drop(
        sparse_grid_table.index[sentinel_indices], axis=0, inplace=False)


def _remove_sentinels_from_full_grid(field_matrix, sentinel_values):
    """Removes sentinel values from full grid.

    M = number of rows (unique grid-point latitudes)
    N = number of columns (unique grid-point longitudes)

    :param field_matrix: M-by-N numpy array with radar field.
    :param sentinel_values: 1-D numpy array of sentinel values.
    :return: field_matrix: Same as input, except that sentinel values are
        replaced with NaN.
    """

    num_grid_rows = field_matrix.shape[0]
    num_grid_columns = field_matrix.shape[1]
    num_grid_points = num_grid_rows * num_grid_columns

    field_matrix = numpy.reshape(field_matrix, num_grid_points)
    sentinel_flags = numpy.full(num_grid_points, False, dtype=bool)

    for this_sentinel_value in sentinel_values:
        these_sentinel_flags = numpy.isclose(
            field_matrix, this_sentinel_value, atol=SENTINEL_TOLERANCE)
        sentinel_flags = numpy.logical_or(sentinel_flags, these_sentinel_flags)

    sentinel_indices = numpy.where(sentinel_flags)[0]
    field_matrix[sentinel_indices] = numpy.nan
    return numpy.reshape(field_matrix, (num_grid_rows, num_grid_columns))


def get_relative_dir_for_raw_files(field_name, data_source, height_m_asl=None):
    """Generates relative path for raw files.

    :param field_name: Name of radar field in GewitterGefahr format.
    :param data_source: Data source (string).
    :param height_m_asl: Radar height (metres above sea level).
    :return: relative_directory_name: Relative path for raw files.
    """

    if field_name == radar_utils.REFL_NAME:
        radar_utils.check_heights(
            data_source=data_source, heights_m_asl=numpy.array([height_m_asl]),
            field_name=radar_utils.REFL_NAME)
    else:
        height_m_asl = radar_utils.get_valid_heights(
            data_source=data_source, field_name=field_name)[0]

    return '{0:s}/{1:05.2f}'.format(
        radar_utils.field_name_new_to_orig(
            field_name=field_name, data_source_name=data_source),
        float(height_m_asl) * METRES_TO_KM
    )


def find_raw_file(
        unix_time_sec, spc_date_string, field_name, data_source,
        top_directory_name, height_m_asl=None, raise_error_if_missing=True):
    """Finds raw file.

    File should contain one field at one time step (e.g., MESH at 123502 UTC,
    reflectivity at 500 m above sea level and 123502 UTC).

    :param unix_time_sec: Valid time.
    :param spc_date_string: SPC date (format "yyyymmdd").
    :param field_name: Name of radar field in GewitterGefahr format.
    :param data_source: Data source (string).
    :param top_directory_name: Name of top-level directory with raw files.
    :param height_m_asl: Radar height (metres above sea level).
    :param raise_error_if_missing: Boolean flag.  If True and file is missing,
        this method will raise an error.  If False and file is missing, will
        return *expected* path to raw file.
    :return: raw_file_name: Path to raw file.
    :raises: ValueError: if raise_error_if_missing = True and file is missing.
    """

    # Error-checking.
    _ = time_conversion.spc_date_string_to_unix_sec(spc_date_string)
    error_checking.assert_is_string(top_directory_name)
    error_checking.assert_is_boolean(raise_error_if_missing)

    relative_directory_name = get_relative_dir_for_raw_files(
        field_name=field_name, height_m_asl=height_m_asl,
        data_source=data_source)

    directory_name = '{0:s}/{1:s}/{2:s}/{3:s}'.format(
        top_directory_name, spc_date_string[:4], spc_date_string,
        relative_directory_name
    )

    pathless_file_name = _get_pathless_raw_file_name(unix_time_sec, zipped=True)
    raw_file_name = '{0:s}/{1:s}'.format(directory_name, pathless_file_name)

    if raise_error_if_missing and not os.path.isfile(raw_file_name):
        pathless_file_name = _get_pathless_raw_file_name(
            unix_time_sec, zipped=False)
        raw_file_name = '{0:s}/{1:s}'.format(directory_name, pathless_file_name)

    if raise_error_if_missing and not os.path.isfile(raw_file_name):
        raise ValueError(
            'Cannot find raw file.  Expected at: "{0:s}"'.format(raw_file_name)
        )

    return raw_file_name


def raw_file_name_to_time(raw_file_name):
    """Parses time from file name.

    :param raw_file_name: Path to raw file.
    :return: unix_time_sec: Valid time.
    """

    error_checking.assert_is_string(raw_file_name)

    _, time_string = os.path.split(raw_file_name)
    time_string = time_string.replace(ZIPPED_FILE_EXTENSION, '').replace(
        UNZIPPED_FILE_EXTENSION, '')

    return time_conversion.string_to_unix_sec(time_string, TIME_FORMAT_SECONDS)


def find_raw_file_inexact_time(
        desired_time_unix_sec, spc_date_string, field_name, data_source,
        top_directory_name, height_m_asl=None, max_time_offset_sec=None,
        raise_error_if_missing=False):
    """Finds raw file at inexact time.

    If you know the exact valid time, use `find_raw_file`.

    :param desired_time_unix_sec: Desired valid time.
    :param spc_date_string: SPC date (format "yyyymmdd").
    :param field_name: Field name in GewitterGefahr format.
    :param data_source: Data source (string).
    :param top_directory_name: Name of top-level directory with raw files.
    :param height_m_asl: Radar height (metres above sea level).
    :param max_time_offset_sec: Maximum offset between actual and desired valid
        time.

    For example, if `desired_time_unix_sec` is 162933 UTC 5 Jan 2018 and
    `max_time_offset_sec` = 60, this method will look for az-shear at valid
    times from 162833...163033 UTC 5 Jan 2018.

    If None, this defaults to `DEFAULT_MAX_TIME_OFFSET_FOR_AZ_SHEAR_SEC` for
    azimuthal-shear fields and `DEFAULT_MAX_TIME_OFFSET_FOR_NON_SHEAR_SEC` for
    all other fields.

    :param raise_error_if_missing: Boolean flag.  If no file is found and
        raise_error_if_missing = True, this method will error out.  If no file
        is found and raise_error_if_missing = False, will return None.
    :return: raw_file_name: Path to raw file.
    :raises: ValueError: if no file is found and raise_error_if_missing = True.
    """

    # Error-checking.
    error_checking.assert_is_integer(desired_time_unix_sec)
    _ = time_conversion.spc_date_string_to_unix_sec(spc_date_string)
    error_checking.assert_is_boolean(raise_error_if_missing)

    radar_utils.check_field_name(field_name)
    if max_time_offset_sec is None:
        if field_name in AZIMUTHAL_SHEAR_FIELD_NAMES:
            max_time_offset_sec = DEFAULT_MAX_TIME_OFFSET_FOR_AZ_SHEAR_SEC
        else:
            max_time_offset_sec = DEFAULT_MAX_TIME_OFFSET_FOR_NON_SHEAR_SEC

    error_checking.assert_is_integer(max_time_offset_sec)
    error_checking.assert_is_greater(max_time_offset_sec, 0)

    first_allowed_minute_unix_sec = numpy.round(int(rounder.floor_to_nearest(
        float(desired_time_unix_sec - max_time_offset_sec),
        MINUTES_TO_SECONDS)))
    last_allowed_minute_unix_sec = numpy.round(int(rounder.floor_to_nearest(
        float(desired_time_unix_sec + max_time_offset_sec),
        MINUTES_TO_SECONDS)))

    allowed_minutes_unix_sec = time_periods.range_and_interval_to_list(
        start_time_unix_sec=first_allowed_minute_unix_sec,
        end_time_unix_sec=last_allowed_minute_unix_sec,
        time_interval_sec=MINUTES_TO_SECONDS, include_endpoint=True).astype(int)

    relative_directory_name = get_relative_dir_for_raw_files(
        field_name=field_name, data_source=data_source,
        height_m_asl=height_m_asl)

    raw_file_names = []
    for this_time_unix_sec in allowed_minutes_unix_sec:
        this_pathless_file_pattern = _get_pathless_raw_file_pattern(
            this_time_unix_sec)

        this_file_pattern = '{0:s}/{1:s}/{2:s}/{3:s}/{4:s}'.format(
            top_directory_name, spc_date_string[:4], spc_date_string,
            relative_directory_name, this_pathless_file_pattern
        )

        raw_file_names += glob.glob(this_file_pattern)

    file_times_unix_sec = []
    for this_raw_file_name in raw_file_names:
        file_times_unix_sec.append(raw_file_name_to_time(this_raw_file_name))

    if len(file_times_unix_sec):
        file_times_unix_sec = numpy.array(file_times_unix_sec)
        time_differences_sec = numpy.absolute(
            file_times_unix_sec - desired_time_unix_sec)
        nearest_index = numpy.argmin(time_differences_sec)
        min_time_diff_sec = time_differences_sec[nearest_index]
    else:
        min_time_diff_sec = numpy.inf

    if min_time_diff_sec > max_time_offset_sec:
        if raise_error_if_missing:
            desired_time_string = time_conversion.unix_sec_to_string(
                desired_time_unix_sec, TIME_FORMAT_FOR_LOG_MESSAGES)

            error_string = (
                'Could not find "{0:s}" file within {1:d} seconds of {2:s}.'
            ).format(field_name, max_time_offset_sec, desired_time_string)

            raise ValueError(error_string)

        return None

    return raw_file_names[nearest_index]


def find_raw_files_one_spc_date(
        spc_date_string, field_name, data_source, top_directory_name,
        height_m_asl=None, raise_error_if_missing=True):
    """Finds raw files for one field and one SPC date.

    :param spc_date_string: SPC date (format "yyyymmdd").
    :param field_name: Name of radar field in GewitterGefahr format.
    :param data_source: Data source (string).
    :param top_directory_name: Name of top-level directory with raw files.
    :param height_m_asl: Radar height (metres above sea level).
    :param raise_error_if_missing: Boolean flag.  If True and no files are
        found, will raise error.
    :return: raw_file_names: 1-D list of paths to raw files.
    :raises: ValueError: if raise_error_if_missing = True and no files are
        found.
    """

    error_checking.assert_is_boolean(raise_error_if_missing)

    example_time_unix_sec = time_conversion.spc_date_string_to_unix_sec(
        spc_date_string)
    example_file_name = find_raw_file(
        unix_time_sec=example_time_unix_sec, spc_date_string=spc_date_string,
        field_name=field_name, data_source=data_source,
        top_directory_name=top_directory_name, height_m_asl=height_m_asl,
        raise_error_if_missing=False)

    example_directory_name, example_pathless_file_name = os.path.split(
        example_file_name)
    example_time_string = time_conversion.unix_sec_to_string(
        example_time_unix_sec, TIME_FORMAT_SECONDS)
    pathless_file_pattern = example_pathless_file_name.replace(
        example_time_string, TIME_FORMAT_SECONDS_REGEX)
    pathless_file_pattern = pathless_file_pattern.replace(
        ZIPPED_FILE_EXTENSION, '*')

    raw_file_pattern = '{0:s}/{1:s}'.format(
        example_directory_name, pathless_file_pattern)
    raw_file_names = glob.glob(raw_file_pattern)

    if raise_error_if_missing and not raw_file_names:
        error_string = (
            'Could not find any files with the following pattern: {0:s}'
        ).format(raw_file_pattern)

        raise ValueError(error_string)

    return raw_file_names


def find_many_raw_files(
        desired_times_unix_sec, spc_date_strings, data_source, field_names,
        top_directory_name, reflectivity_heights_m_asl=None,
        max_time_offset_for_az_shear_sec=
        DEFAULT_MAX_TIME_OFFSET_FOR_AZ_SHEAR_SEC,
        max_time_offset_for_non_shear_sec=
        DEFAULT_MAX_TIME_OFFSET_FOR_NON_SHEAR_SEC):
    """Finds raw file for each field/height pair and time step.

    N = number of input times
    T = number of unique input times
    F = number of field/height pairs

    :param desired_times_unix_sec: length-N numpy array with desired valid
        times.
    :param spc_date_strings: length-N list of corresponding SPC dates (format
        "yyyymmdd").
    :param data_source: Data source ("myrorss" or "mrms").
    :param field_names: 1-D list of field names.
    :param top_directory_name: Name of top-level directory with radar data from
        the given source.
    :param reflectivity_heights_m_asl: 1-D numpy array of heights (metres above
        sea level) for the field "reflectivity_dbz".  If "reflectivity_dbz" is
        not in `field_names`, leave this as None.
    :param max_time_offset_for_az_shear_sec: Max time offset (between desired
        and actual valid time) for azimuthal-shear fields.
    :param max_time_offset_for_non_shear_sec: Max time offset (between desired
        and actual valid time) for non-azimuthal-shear fields.
    :return: file_dictionary: Dictionary with the following keys.
    file_dictionary['radar_file_name_matrix']: T-by-F numpy array of paths to
        raw files.
    file_dictionary['unique_times_unix_sec']: length-T numpy array of unique
        valid times.
    file_dictionary['spc_date_strings_for_unique_times']: length-T numpy array
        of corresponding SPC dates.
    file_dictionary['field_name_by_pair']: length-F list of field names.
    file_dictionary['height_by_pair_m_asl']: length-F numpy array of heights
        (metres above sea level).
    """

    field_name_by_pair, height_by_pair_m_asl = (
        myrorss_and_mrms_utils.fields_and_refl_heights_to_pairs(
            field_names=field_names, data_source=data_source,
            refl_heights_m_asl=reflectivity_heights_m_asl)
    )

    num_fields = len(field_name_by_pair)

    error_checking.assert_is_integer_numpy_array(desired_times_unix_sec)
    error_checking.assert_is_numpy_array(
        desired_times_unix_sec, num_dimensions=1)
    num_times = len(desired_times_unix_sec)

    error_checking.assert_is_string_list(spc_date_strings)
    error_checking.assert_is_numpy_array(
        numpy.array(spc_date_strings),
        exact_dimensions=numpy.array([num_times]))

    spc_dates_unix_sec = numpy.array(
        [time_conversion.spc_date_string_to_unix_sec(s)
         for s in spc_date_strings])

    time_matrix = numpy.hstack((
        numpy.reshape(desired_times_unix_sec, (num_times, 1)),
        numpy.reshape(spc_dates_unix_sec, (num_times, 1))
    ))

    unique_time_matrix = numpy.vstack(
        {tuple(this_row) for this_row in time_matrix}
    ).astype(int)

    unique_times_unix_sec = unique_time_matrix[:, 0]
    spc_dates_at_unique_times_unix_sec = unique_time_matrix[:, 1]

    sort_indices = numpy.argsort(unique_times_unix_sec)
    unique_times_unix_sec = unique_times_unix_sec[sort_indices]
    spc_dates_at_unique_times_unix_sec = spc_dates_at_unique_times_unix_sec[
        sort_indices]

    num_unique_times = len(unique_times_unix_sec)
    radar_file_name_matrix = numpy.full(
        (num_unique_times, num_fields), '', dtype=object)

    for i in range(num_unique_times):
        this_spc_date_string = time_conversion.time_to_spc_date_string(
            spc_dates_at_unique_times_unix_sec[i])

        for j in range(num_fields):
            if field_name_by_pair[j] in AZIMUTHAL_SHEAR_FIELD_NAMES:
                this_max_time_offset_sec = max_time_offset_for_az_shear_sec
                this_raise_error_flag = False
            else:
                this_max_time_offset_sec = max_time_offset_for_non_shear_sec
                this_raise_error_flag = True

            if this_max_time_offset_sec == 0:
                radar_file_name_matrix[i, j] = find_raw_file(
                    unix_time_sec=unique_times_unix_sec[i],
                    spc_date_string=this_spc_date_string,
                    field_name=field_name_by_pair[j], data_source=data_source,
                    top_directory_name=top_directory_name,
                    height_m_asl=height_by_pair_m_asl[j],
                    raise_error_if_missing=this_raise_error_flag)
            else:
                radar_file_name_matrix[i, j] = find_raw_file_inexact_time(
                    desired_time_unix_sec=unique_times_unix_sec[i],
                    spc_date_string=this_spc_date_string,
                    field_name=field_name_by_pair[j], data_source=data_source,
                    top_directory_name=top_directory_name,
                    height_m_asl=height_by_pair_m_asl[j],
                    max_time_offset_sec=this_max_time_offset_sec,
                    raise_error_if_missing=this_raise_error_flag)

            if radar_file_name_matrix[i, j] is None:
                this_time_string = time_conversion.unix_sec_to_string(
                    unique_times_unix_sec[i], TIME_FORMAT_FOR_LOG_MESSAGES)

                warning_string = (
                    'Cannot find file for "{0:s}" at {1:d} metres ASL and '
                    '{2:s}.'
                ).format(
                    field_name_by_pair[j], int(height_by_pair_m_asl[j]),
                    this_time_string
                )

                warnings.warn(warning_string)

    return {
        RADAR_FILE_NAMES_KEY: radar_file_name_matrix,
        UNIQUE_TIMES_KEY: unique_times_unix_sec,
        SPC_DATES_AT_UNIQUE_TIMES_KEY: spc_dates_at_unique_times_unix_sec,
        FIELD_NAME_BY_PAIR_KEY: field_name_by_pair,
        HEIGHT_BY_PAIR_KEY: numpy.round(height_by_pair_m_asl).astype(int)
    }


def read_metadata_from_raw_file(
        netcdf_file_name, data_source, raise_error_if_fails=True):
    """Reads metadata from raw (either MYRORSS or MRMS) file.

    This file should contain one radar field at one height and valid time.

    :param netcdf_file_name: Path to input file.
    :param data_source: Data source (string).
    :param raise_error_if_fails: Boolean flag.  If True and file cannot be read,
        this method will raise an error.  If False and file cannot be read, will
        return None.
    :return: metadata_dict: Dictionary with the following keys.
    metadata_dict['nw_grid_point_lat_deg']: Latitude (deg N) of northwesternmost
        grid point.
    metadata_dict['nw_grid_point_lng_deg']: Longitude (deg E) of
        northwesternmost grid point.
    metadata_dict['lat_spacing_deg']: Spacing (deg N) between meridionally
        adjacent grid points.
    metadata_dict['lng_spacing_deg']: Spacing (deg E) between zonally adjacent
        grid points.
    metadata_dict['num_lat_in_grid']: Number of rows (unique grid-point
        latitudes).
    metadata_dict['num_lng_in_grid']: Number of columns (unique grid-point
        longitudes).
    metadata_dict['height_m_asl']: Radar height (metres above ground level).
    metadata_dict['unix_time_sec']: Valid time.
    metadata_dict['field_name']: Name of radar field in GewitterGefahr format.
    metadata_dict['field_name_orig']: Name of radar field in original (either
        MYRORSS or MRMS) format.
    metadata_dict['sentinel_values']: 1-D numpy array of sentinel values.
    """

    error_checking.assert_file_exists(netcdf_file_name)
    netcdf_dataset = netcdf_io.open_netcdf(
        netcdf_file_name, raise_error_if_fails)
    if netcdf_dataset is None:
        return None

    field_name_orig = str(getattr(netcdf_dataset, FIELD_NAME_COLUMN_ORIG))

    metadata_dict = {
        radar_utils.NW_GRID_POINT_LAT_COLUMN:
            getattr(netcdf_dataset, NW_GRID_POINT_LAT_COLUMN_ORIG),
        radar_utils.NW_GRID_POINT_LNG_COLUMN:
            lng_conversion.convert_lng_positive_in_west(
                getattr(netcdf_dataset, NW_GRID_POINT_LNG_COLUMN_ORIG),
                allow_nan=False),
        radar_utils.LAT_SPACING_COLUMN:
            getattr(netcdf_dataset, LAT_SPACING_COLUMN_ORIG),
        radar_utils.LNG_SPACING_COLUMN:
            getattr(netcdf_dataset, LNG_SPACING_COLUMN_ORIG),
        radar_utils.NUM_LAT_COLUMN:
            netcdf_dataset.dimensions[NUM_LAT_COLUMN_ORIG].size + 1,
        radar_utils.NUM_LNG_COLUMN:
            netcdf_dataset.dimensions[NUM_LNG_COLUMN_ORIG].size + 1,
        radar_utils.HEIGHT_COLUMN:
            getattr(netcdf_dataset, HEIGHT_COLUMN_ORIG),
        radar_utils.UNIX_TIME_COLUMN:
            getattr(netcdf_dataset, UNIX_TIME_COLUMN_ORIG),
        FIELD_NAME_COLUMN_ORIG: field_name_orig,
        radar_utils.FIELD_NAME_COLUMN: radar_utils.field_name_orig_to_new(
            field_name_orig=field_name_orig, data_source_name=data_source)
    }

    latitude_spacing_deg = metadata_dict[radar_utils.LAT_SPACING_COLUMN]
    longitude_spacing_deg = metadata_dict[radar_utils.LNG_SPACING_COLUMN]

    # TODO(thunderhoser): The following "if" condition is a hack.  The purpose
    # is to change grid corners only for actual MYRORSS data, not GridRad data
    # in MYRORSS format.
    if latitude_spacing_deg < 0.011 and longitude_spacing_deg < 0.011:
        metadata_dict[radar_utils.NW_GRID_POINT_LAT_COLUMN] = (
            rounder.floor_to_nearest(
                metadata_dict[radar_utils.NW_GRID_POINT_LAT_COLUMN],
                metadata_dict[radar_utils.LAT_SPACING_COLUMN]))
        metadata_dict[radar_utils.NW_GRID_POINT_LNG_COLUMN] = (
            rounder.ceiling_to_nearest(
                metadata_dict[radar_utils.NW_GRID_POINT_LNG_COLUMN],
                metadata_dict[radar_utils.LNG_SPACING_COLUMN]))

    sentinel_values = []
    for this_column in SENTINEL_VALUE_COLUMNS_ORIG:
        sentinel_values.append(getattr(netcdf_dataset, this_column))

    metadata_dict.update({
        radar_utils.SENTINEL_VALUE_COLUMN: numpy.array(sentinel_values)})
    netcdf_dataset.close()
    return metadata_dict


def read_data_from_sparse_grid_file(
        netcdf_file_name, field_name_orig, data_source, sentinel_values,
        raise_error_if_fails=True):
    """Reads sparse radar grid from raw (either MYRORSS or MRMS) file.

    This file should contain one radar field at one height and valid time.

    :param netcdf_file_name: Path to input file.
    :param field_name_orig: Name of radar field in original (either MYRORSS or
        MRMS) format.
    :param data_source: Data source (string).
    :param sentinel_values: 1-D numpy array of sentinel values.
    :param raise_error_if_fails: Boolean flag.  If True and file cannot be read,
        this method will raise an error.  If False and file cannot be read, will
        return None.
    :return: sparse_grid_table: pandas DataFrame with the following columns.
        Each row corresponds to one grid point.
    sparse_grid_table.grid_row: Row index.
    sparse_grid_table.grid_column: Column index.
    sparse_grid_table.<field_name>: Radar measurement (column name is produced
        by _field_name_orig_to_new).
    sparse_grid_table.num_grid_cells: Number of consecutive grid points with the
        same radar measurement.  Counting is row-major (to the right along the
        row, then down to the next column if necessary).
    """

    error_checking.assert_file_exists(netcdf_file_name)
    error_checking.assert_is_numpy_array_without_nan(sentinel_values)
    error_checking.assert_is_numpy_array(sentinel_values, num_dimensions=1)

    netcdf_dataset = netcdf_io.open_netcdf(
        netcdf_file_name, raise_error_if_fails)
    if netcdf_dataset is None:
        return None

    field_name = radar_utils.field_name_orig_to_new(
        field_name_orig=field_name_orig, data_source_name=data_source)
    num_values = len(netcdf_dataset.variables[GRID_ROW_COLUMN_ORIG])

    if num_values == 0:
        sparse_grid_dict = {
            GRID_ROW_COLUMN: numpy.array([], dtype=int),
            GRID_COLUMN_COLUMN: numpy.array([], dtype=int),
            NUM_GRID_CELL_COLUMN: numpy.array([], dtype=int),
            field_name: numpy.array([])}
    else:
        sparse_grid_dict = {
            GRID_ROW_COLUMN: netcdf_dataset.variables[GRID_ROW_COLUMN_ORIG][:],
            GRID_COLUMN_COLUMN:
                netcdf_dataset.variables[GRID_COLUMN_COLUMN_ORIG][:],
            NUM_GRID_CELL_COLUMN:
                netcdf_dataset.variables[NUM_GRID_CELL_COLUMN_ORIG][:],
            field_name: netcdf_dataset.variables[field_name_orig][:]}

    netcdf_dataset.close()
    sparse_grid_table = pandas.DataFrame.from_dict(sparse_grid_dict)
    return _remove_sentinels_from_sparse_grid(
        sparse_grid_table, field_name=field_name,
        sentinel_values=sentinel_values)


def read_data_from_full_grid_file(
        netcdf_file_name, metadata_dict, raise_error_if_fails=True):
    """Reads full radar grid from raw (either MYRORSS or MRMS) file.

    This file should contain one radar field at one height and valid time.

    :param netcdf_file_name: Path to input file.
    :param metadata_dict: Dictionary created by `read_metadata_from_raw_file`.
    :param raise_error_if_fails: Boolean flag.  If True and file cannot be read,
        this method will raise an error.  If False and file cannot be read, will
        return None for all output vars.
    :return: field_matrix: M-by-N numpy array with radar field.  Latitude
        increases while moving up each column, and longitude increases while
        moving right along each row.
    :return: grid_point_latitudes_deg: length-M numpy array of grid-point
        latitudes (deg N).  This array is monotonically decreasing.
    :return: grid_point_longitudes_deg: length-N numpy array of grid-point
        longitudes (deg E).  This array is monotonically increasing.
    """

    error_checking.assert_file_exists(netcdf_file_name)
    netcdf_dataset = netcdf_io.open_netcdf(
        netcdf_file_name, raise_error_if_fails)
    if netcdf_dataset is None:
        return None, None, None

    field_matrix = netcdf_dataset.variables[
        metadata_dict[FIELD_NAME_COLUMN_ORIG]]
    netcdf_dataset.close()

    min_latitude_deg = metadata_dict[radar_utils.NW_GRID_POINT_LAT_COLUMN] - (
        metadata_dict[radar_utils.LAT_SPACING_COLUMN] * (
            metadata_dict[radar_utils.NUM_LAT_COLUMN] - 1))
    grid_point_latitudes_deg, grid_point_longitudes_deg = (
        grids.get_latlng_grid_points(
            min_latitude_deg=min_latitude_deg,
            min_longitude_deg=
            metadata_dict[radar_utils.NW_GRID_POINT_LNG_COLUMN],
            lat_spacing_deg=metadata_dict[radar_utils.LAT_SPACING_COLUMN],
            lng_spacing_deg=metadata_dict[radar_utils.LNG_SPACING_COLUMN],
            num_rows=metadata_dict[radar_utils.NUM_LAT_COLUMN],
            num_columns=metadata_dict[radar_utils.NUM_LNG_COLUMN]))

    field_matrix = _remove_sentinels_from_full_grid(
        field_matrix, metadata_dict[radar_utils.SENTINEL_VALUE_COLUMN])
    return (numpy.flipud(field_matrix), grid_point_latitudes_deg[::-1],
            grid_point_longitudes_deg)


def write_field_to_myrorss_file(
        field_matrix, netcdf_file_name, field_name, metadata_dict,
        height_m_asl=None):
    """Writes field to MYRORSS-formatted file.

    M = number of rows (unique grid-point latitudes)
    N = number of columns (unique grid-point longitudes)

    :param field_matrix: M-by-N numpy array with one radar variable at one time.
        Latitude should increase down each column, and longitude should increase
        to the right along each row.
    :param netcdf_file_name: Path to output file.
    :param field_name: Name of radar field in GewitterGefahr format.
    :param metadata_dict: Dictionary created by either
        `gridrad_io.read_metadata_from_full_grid_file` or
        `read_metadata_from_raw_file`.
    :param height_m_asl: Height of radar field (metres above sea level).
    """

    if field_name == radar_utils.REFL_NAME:
        field_to_heights_dict_m_asl = (
            myrorss_and_mrms_utils.fields_and_refl_heights_to_dict(
                field_names=[field_name],
                data_source=radar_utils.MYRORSS_SOURCE_ID,
                refl_heights_m_asl=numpy.array([height_m_asl])))

    else:
        field_to_heights_dict_m_asl = (
            myrorss_and_mrms_utils.fields_and_refl_heights_to_dict(
                field_names=[field_name],
                data_source=radar_utils.MYRORSS_SOURCE_ID))

    field_name = list(field_to_heights_dict_m_asl.keys())[0]
    radar_height_m_asl = field_to_heights_dict_m_asl[field_name][0]

    if field_name in radar_utils.ECHO_TOP_NAMES:
        field_matrix = METRES_TO_KM * field_matrix
    field_name_myrorss = radar_utils.field_name_new_to_orig(
        field_name=field_name, data_source_name=radar_utils.MYRORSS_SOURCE_ID)

    file_system_utils.mkdir_recursive_if_necessary(file_name=netcdf_file_name)
    netcdf_dataset = Dataset(
        netcdf_file_name, 'w', format='NETCDF3_64BIT_OFFSET')

    netcdf_dataset.setncattr(
        FIELD_NAME_COLUMN_ORIG, field_name_myrorss)
    netcdf_dataset.setncattr('DataType', 'SparseLatLonGrid')

    netcdf_dataset.setncattr(
        NW_GRID_POINT_LAT_COLUMN_ORIG, rounder.round_to_nearest(
            metadata_dict[radar_utils.NW_GRID_POINT_LAT_COLUMN],
            LATLNG_MULTIPLE_DEG))
    netcdf_dataset.setncattr(
        NW_GRID_POINT_LNG_COLUMN_ORIG, rounder.round_to_nearest(
            metadata_dict[radar_utils.NW_GRID_POINT_LNG_COLUMN],
            LATLNG_MULTIPLE_DEG))
    netcdf_dataset.setncattr(
        HEIGHT_COLUMN_ORIG,
        METRES_TO_KM * numpy.float(radar_height_m_asl))
    netcdf_dataset.setncattr(
        UNIX_TIME_COLUMN_ORIG,
        numpy.int32(metadata_dict[radar_utils.UNIX_TIME_COLUMN]))
    netcdf_dataset.setncattr('FractionalTime', 0.)

    netcdf_dataset.setncattr('attributes', ' ColorMap SubType Unit')
    netcdf_dataset.setncattr('ColorMap-unit', 'dimensionless')
    netcdf_dataset.setncattr('ColorMap-value', '')
    netcdf_dataset.setncattr('SubType-unit', 'dimensionless')
    netcdf_dataset.setncattr('SubType-value', numpy.float(radar_height_m_asl))
    netcdf_dataset.setncattr('Unit-unit', 'dimensionless')
    netcdf_dataset.setncattr('Unit-value', 'dimensionless')

    netcdf_dataset.setncattr(
        LAT_SPACING_COLUMN_ORIG, rounder.round_to_nearest(
            metadata_dict[radar_utils.LAT_SPACING_COLUMN],
            LATLNG_MULTIPLE_DEG))
    netcdf_dataset.setncattr(
        LNG_SPACING_COLUMN_ORIG, rounder.round_to_nearest(
            metadata_dict[radar_utils.LNG_SPACING_COLUMN],
            LATLNG_MULTIPLE_DEG))
    netcdf_dataset.setncattr(
        SENTINEL_VALUE_COLUMNS_ORIG[0], numpy.double(-99000.))
    netcdf_dataset.setncattr(
        SENTINEL_VALUE_COLUMNS_ORIG[1], numpy.double(-99001.))

    min_latitude_deg = metadata_dict[radar_utils.NW_GRID_POINT_LAT_COLUMN] - (
        metadata_dict[radar_utils.LAT_SPACING_COLUMN] *
        (metadata_dict[radar_utils.NUM_LAT_COLUMN] - 1))
    unique_grid_point_lats_deg, unique_grid_point_lngs_deg = (
        grids.get_latlng_grid_points(
            min_latitude_deg=min_latitude_deg,
            min_longitude_deg=
            metadata_dict[radar_utils.NW_GRID_POINT_LNG_COLUMN],
            lat_spacing_deg=metadata_dict[radar_utils.LAT_SPACING_COLUMN],
            lng_spacing_deg=metadata_dict[radar_utils.LNG_SPACING_COLUMN],
            num_rows=metadata_dict[radar_utils.NUM_LAT_COLUMN],
            num_columns=metadata_dict[radar_utils.NUM_LNG_COLUMN]))

    num_grid_rows = len(unique_grid_point_lats_deg)
    num_grid_columns = len(unique_grid_point_lngs_deg)
    field_vector = numpy.reshape(field_matrix, num_grid_rows * num_grid_columns)

    grid_point_lat_matrix, grid_point_lng_matrix = (
        grids.latlng_vectors_to_matrices(
            unique_grid_point_lats_deg, unique_grid_point_lngs_deg))
    grid_point_lat_vector = numpy.reshape(
        grid_point_lat_matrix, num_grid_rows * num_grid_columns)
    grid_point_lng_vector = numpy.reshape(
        grid_point_lng_matrix, num_grid_rows * num_grid_columns)

    real_value_indices = numpy.where(numpy.invert(numpy.isnan(field_vector)))[0]
    netcdf_dataset.createDimension(
        NUM_LAT_COLUMN_ORIG, num_grid_rows - 1)
    netcdf_dataset.createDimension(
        NUM_LNG_COLUMN_ORIG, num_grid_columns - 1)
    netcdf_dataset.createDimension(
        NUM_PIXELS_COLUMN_ORIG, len(real_value_indices))

    row_index_vector, column_index_vector = radar_utils.latlng_to_rowcol(
        grid_point_lat_vector, grid_point_lng_vector,
        nw_grid_point_lat_deg=
        metadata_dict[radar_utils.NW_GRID_POINT_LAT_COLUMN],
        nw_grid_point_lng_deg=
        metadata_dict[radar_utils.NW_GRID_POINT_LNG_COLUMN],
        lat_spacing_deg=metadata_dict[radar_utils.LAT_SPACING_COLUMN],
        lng_spacing_deg=metadata_dict[radar_utils.LNG_SPACING_COLUMN])

    netcdf_dataset.createVariable(
        field_name_myrorss, numpy.single, (NUM_PIXELS_COLUMN_ORIG,))
    netcdf_dataset.createVariable(
        GRID_ROW_COLUMN_ORIG, numpy.int16, (NUM_PIXELS_COLUMN_ORIG,))
    netcdf_dataset.createVariable(
        GRID_COLUMN_COLUMN_ORIG, numpy.int16, (NUM_PIXELS_COLUMN_ORIG,))
    netcdf_dataset.createVariable(
        NUM_GRID_CELL_COLUMN_ORIG, numpy.int32, (NUM_PIXELS_COLUMN_ORIG,))

    netcdf_dataset.variables[field_name_myrorss].setncattr(
        'BackgroundValue', numpy.int32(-99900))
    netcdf_dataset.variables[field_name_myrorss].setncattr(
        'units', 'dimensionless')
    netcdf_dataset.variables[field_name_myrorss].setncattr(
        'NumValidRuns', numpy.int32(len(real_value_indices)))

    netcdf_dataset.variables[field_name_myrorss][:] = field_vector[
        real_value_indices]
    netcdf_dataset.variables[GRID_ROW_COLUMN_ORIG][:] = (
        row_index_vector[real_value_indices])
    netcdf_dataset.variables[GRID_COLUMN_COLUMN_ORIG][:] = (
        column_index_vector[real_value_indices])
    netcdf_dataset.variables[NUM_GRID_CELL_COLUMN_ORIG][:] = (
        numpy.full(len(real_value_indices), 1, dtype=int))

    netcdf_dataset.close()
