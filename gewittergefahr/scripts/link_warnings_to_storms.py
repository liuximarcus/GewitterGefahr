"""Links each NWS tornado warning to nearest storm."""

import copy
import pickle
import argparse
import numpy
import shapely.geometry
from gewittergefahr.gg_io import storm_tracking_io as tracking_io
from gewittergefahr.gg_utils import storm_tracking_utils as tracking_utils
from gewittergefahr.gg_utils import time_conversion
from gewittergefahr.gg_utils import time_periods
from gewittergefahr.gg_utils import echo_top_tracking
from gewittergefahr.gg_utils import polygons
from gewittergefahr.gg_utils import projections
from gewittergefahr.gg_utils import linkage
from gewittergefahr.gg_utils import file_system_utils
from gewittergefahr.gg_utils import error_checking
from gewittergefahr.nature2019 import convert_warning_polygons

SEPARATOR_STRING = '\n\n' + '*' * 50 + '\n\n'
LOG_MESSAGE_TIME_FORMAT = '%Y-%m-%d-%H%M'

LARGE_NUMBER = 1e12
NUM_SECONDS_PER_DAY = 86400
DUMMY_TRACKING_SCALE_METRES2 = echo_top_tracking.DUMMY_TRACKING_SCALE_METRES2

PROJECTION_OBJECT = projections.init_azimuthal_equidistant_projection(
    central_latitude_deg=35., central_longitude_deg=265.
)

WARNING_START_TIME_KEY = convert_warning_polygons.START_TIME_COLUMN
WARNING_END_TIME_KEY = convert_warning_polygons.END_TIME_COLUMN
WARNING_LATLNG_POLYGON_KEY = convert_warning_polygons.POLYGON_COLUMN
WARNING_XY_POLYGON_KEY = 'polygon_object_xy'
LINKED_SECONDARY_IDS_KEY = 'linked_sec_id_strings'

INPUT_WARNING_FILE_ARG_NAME = 'input_warning_file_name'
TRACKING_DIR_ARG_NAME = 'input_tracking_dir_name'
SPC_DATE_ARG_NAME = 'spc_date_string'
MAX_DISTANCE_ARG_NAME = 'max_distance_metres'
MIN_LIFETIME_FRACTION_ARG_NAME = 'min_lifetime_fraction'
OUTPUT_WARNING_FILE_ARG_NAME = 'output_warning_file_name'

INPUT_WARNING_FILE_HELP_STRING = (
    'Path to Pickle file with tornado warnings (created by '
    'convert_warning_polygons.py).'
)
TRACKING_DIR_HELP_STRING = (
    'Name of top-level tracking directory.  Files therein will be found by '
    '`storm_tracking_io.find_processed_files_one_spc_date` and read by '
    '`storm_tracking_io.read_processed_file`.'
)
SPC_DATE_HELP_STRING = (
    'SPC date (format "yyyymmdd").  This script will link only warnings that '
    '*begin* on the given SPC date.'
)
MAX_DISTANCE_HELP_STRING = (
    'Max linkage distance.  Will link each warning to the nearest storm, as '
    'long as the storm''s mean distance outside the warning polygon does not '
    'exceed this value.'
)
MIN_LIFETIME_FRACTION_HELP_STRING = (
    'Minimum lifetime fraction.  Will link each warning to the nearest storm, '
    'as long as the storm is in existence for at least this fraction (range '
    '0...1) of the warning period.'
)
OUTPUT_WARNING_FILE_HELP_STRING = (
    'Path to output file (same as input file but with an extra column, '
    '"{0:s}", in the pandas table).'
).format(LINKED_SECONDARY_IDS_KEY)

INPUT_ARG_PARSER = argparse.ArgumentParser()
INPUT_ARG_PARSER.add_argument(
    '--' + INPUT_WARNING_FILE_ARG_NAME, type=str, required=True,
    help=INPUT_WARNING_FILE_HELP_STRING
)
INPUT_ARG_PARSER.add_argument(
    '--' + TRACKING_DIR_ARG_NAME, type=str, required=True,
    help=TRACKING_DIR_HELP_STRING
)
INPUT_ARG_PARSER.add_argument(
    '--' + SPC_DATE_ARG_NAME, type=str, required=True,
    help=SPC_DATE_HELP_STRING
)
INPUT_ARG_PARSER.add_argument(
    '--' + MAX_DISTANCE_ARG_NAME, type=float, required=False, default=5000.,
    help=MAX_DISTANCE_HELP_STRING
)
INPUT_ARG_PARSER.add_argument(
    '--' + MIN_LIFETIME_FRACTION_ARG_NAME, type=float, required=False,
    default=0.5, help=MIN_LIFETIME_FRACTION_HELP_STRING
)
INPUT_ARG_PARSER.add_argument(
    '--' + OUTPUT_WARNING_FILE_ARG_NAME, type=str, required=True,
    help=OUTPUT_WARNING_FILE_HELP_STRING
)


def _remove_far_away_storms(warning_polygon_object_latlng, storm_object_table):
    """Removes storms that are far away from a warning polygon.

    :param warning_polygon_object_latlng: See doc for `_link_one_warning`.
    :param storm_object_table: Same.
    :return: storm_object_table: Same as input but with fewer rows.
    """

    this_vertex_dict = polygons.polygon_object_to_vertex_arrays(
        warning_polygon_object_latlng
    )
    warning_latitudes_deg = this_vertex_dict[polygons.EXTERIOR_Y_COLUMN]
    warning_longitudes_deg = this_vertex_dict[polygons.EXTERIOR_X_COLUMN]

    unique_primary_id_strings = numpy.unique(
        storm_object_table[tracking_utils.PRIMARY_ID_COLUMN].values
    )
    good_indices = []

    for i in range(len(unique_primary_id_strings)):
        these_rows = numpy.where(
            storm_object_table[tracking_utils.PRIMARY_ID_COLUMN].values ==
            unique_primary_id_strings[i]
        )[0]

        these_latitudes_deg = storm_object_table[
            tracking_utils.CENTROID_LATITUDE_COLUMN
        ].values[these_rows]

        these_longitudes_deg = storm_object_table[
            tracking_utils.CENTROID_LONGITUDE_COLUMN
        ].values[these_rows]

        these_latitude_flags = numpy.logical_and(
            these_latitudes_deg >= numpy.min(warning_latitudes_deg) - 1.,
            these_latitudes_deg <= numpy.max(warning_latitudes_deg) + 1.
        )
        these_longitude_flags = numpy.logical_and(
            these_longitudes_deg >= numpy.min(warning_longitudes_deg) - 1.,
            these_longitudes_deg <= numpy.max(warning_longitudes_deg) + 1.
        )
        these_coord_flags = numpy.logical_and(
            these_latitude_flags, these_longitude_flags
        )

        if not numpy.any(these_coord_flags):
            continue

        good_indices.append(i)

    unique_primary_id_strings = [
        unique_primary_id_strings[k] for k in good_indices
    ]

    return storm_object_table.loc[
        storm_object_table[tracking_utils.PRIMARY_ID_COLUMN].isin(
            unique_primary_id_strings
        )
    ]


def _find_one_polygon_distance(
        storm_x_vertices_metres, storm_y_vertices_metres,
        warning_polygon_object_xy):
    """Finds distance between one storm object and one warning.

    V = number of vertices in storm outline

    :param storm_x_vertices_metres: length-V numpy array of x-coordinates.
    :param storm_y_vertices_metres: length-V numpy array of y-coordinates.
    :param warning_polygon_object_xy: Polygon (instance of
        `shapely.geometry.Polygon`) with x-y coordinates of warning boundary.
    :return: distance_metres: Distance between storm object and warning (minimum
        distance to polygon interior over all storm vertices).
    """

    num_vertices = len(storm_x_vertices_metres)
    distance_metres = LARGE_NUMBER

    for k in range(num_vertices):
        this_flag = polygons.point_in_or_on_polygon(
            polygon_object=warning_polygon_object_xy,
            query_x_coordinate=storm_x_vertices_metres[k],
            query_y_coordinate=storm_y_vertices_metres[k]
        )

        if this_flag:
            return 0.

        this_point_object = shapely.geometry.Point(
            storm_x_vertices_metres[k], storm_y_vertices_metres[k]
        )
        this_distance_metres = this_point_object.distance(
            warning_polygon_object_xy
        )
        distance_metres = numpy.minimum(distance_metres, this_distance_metres)

    return distance_metres


def _find_one_centroid_distance(
        storm_x_vertices_metres, storm_y_vertices_metres,
        warning_polygon_object_xy):
    """Finds distance between one storm object and one warning.

    V = number of vertices in storm outline

    :param storm_x_vertices_metres: length-V numpy array of x-coordinates.
    :param storm_y_vertices_metres: length-V numpy array of y-coordinates.
    :param warning_polygon_object_xy: Polygon (instance of
        `shapely.geometry.Polygon`) with x-y coordinates of warning boundary.
    :return: distance_metres: Distance between storm object and warning (minimum
        distance to polygon interior over all storm vertices).
    """

    centroid_x_metres = numpy.mean(storm_x_vertices_metres)
    centroid_y_metres = numpy.mean(storm_y_vertices_metres)

    pip_flag = polygons.point_in_or_on_polygon(
        polygon_object=warning_polygon_object_xy,
        query_x_coordinate=centroid_x_metres,
        query_y_coordinate=centroid_y_metres
    )

    if pip_flag:
        return 0.

    point_object = shapely.geometry.Point(centroid_x_metres, centroid_y_metres)
    return point_object.distance(warning_polygon_object_xy)


def _link_one_warning(warning_table, storm_object_table, max_distance_metres,
                      min_lifetime_fraction, test_mode=False):
    """Links one warning to nearest storm.

    :param warning_table: pandas DataFrame with one row and the following
        columns.
    warning_table.start_time_unix_sec: Start time.
    warning_table.end_time_unix_sec: End time.
    warning_table.polygon_object_latlng: Polygon (instance of
        `shapely.geometry.Polygon`) with lat-long coordinates of warning
        boundary.
    warning_table.polygon_object_xy: Polygon (instance of
        `shapely.geometry.Polygon`) with x-y coordinates of warning boundary.

    :param storm_object_table: pandas DataFrame returned by
        `storm_tracking_io.read_file`.
    :param max_distance_metres: See documentation at top of file.
    :param min_lifetime_fraction: Same.
    :param test_mode: Never mind.  Just leave this alone.
    :return: secondary_id_strings: 1-D list of secondary IDs for storms to which
        warning is linked.  If warning is not linked to a storm, this is empty.
    """

    warning_start_time_unix_sec = (
        warning_table[WARNING_START_TIME_KEY].values[0]
    )
    warning_end_time_unix_sec = warning_table[WARNING_END_TIME_KEY].values[0]
    warning_polygon_object_xy = warning_table[WARNING_XY_POLYGON_KEY].values[0]

    orig_num_storm_objects = len(storm_object_table.index)

    storm_object_table = linkage._filter_storms_by_time(
        storm_object_table=storm_object_table,
        max_start_time_unix_sec=warning_end_time_unix_sec + 720,
        min_end_time_unix_sec=warning_start_time_unix_sec - 720
    )

    num_storm_objects = len(storm_object_table.index)
    print('Filtering by time removed {0:d} of {1:d} storm objects.'.format(
        orig_num_storm_objects - num_storm_objects, orig_num_storm_objects
    ))

    orig_num_storm_objects = num_storm_objects + 0

    storm_object_table = _remove_far_away_storms(
        warning_polygon_object_latlng=
        warning_table[WARNING_LATLNG_POLYGON_KEY].values[0],
        storm_object_table=storm_object_table
    )

    num_storm_objects = len(storm_object_table.index)
    print('Filtering by distance removed {0:d} of {1:d} storm objects.'.format(
        orig_num_storm_objects - num_storm_objects, orig_num_storm_objects
    ))

    warning_times_unix_sec = time_periods.range_and_interval_to_list(
        start_time_unix_sec=warning_start_time_unix_sec,
        end_time_unix_sec=warning_end_time_unix_sec,
        time_interval_sec=1 if test_mode else 60, include_endpoint=True
    )

    unique_sec_id_strings = numpy.unique(
        storm_object_table[tracking_utils.SECONDARY_ID_COLUMN].values
    )

    num_sec_id_strings = len(unique_sec_id_strings)
    num_warning_times = len(warning_times_unix_sec)
    distance_matrix_metres = numpy.full(
        (num_sec_id_strings, num_warning_times), numpy.nan
    )

    for j in range(num_warning_times):
        this_interp_vertex_table = linkage._interp_storms_in_time(
            storm_object_table=storm_object_table,
            target_time_unix_sec=warning_times_unix_sec[j],
            max_time_before_start_sec=0 if test_mode else 180,
            max_time_after_end_sec=0 if test_mode else 180
        )

        for i in range(num_sec_id_strings):
            these_indices = numpy.where(
                this_interp_vertex_table[
                    tracking_utils.SECONDARY_ID_COLUMN].values
                == unique_sec_id_strings[i]
            )[0]

            if len(these_indices) == 0:
                continue

            these_x_metres = this_interp_vertex_table[
                linkage.STORM_VERTEX_X_COLUMN
            ].values[these_indices]

            these_y_metres = this_interp_vertex_table[
                linkage.STORM_VERTEX_Y_COLUMN
            ].values[these_indices]

            distance_matrix_metres[i, j] = _find_one_centroid_distance(
                storm_x_vertices_metres=these_x_metres,
                storm_y_vertices_metres=these_y_metres,
                warning_polygon_object_xy=warning_polygon_object_xy
            )

    lifetime_fractions = (
        1. - numpy.mean(numpy.isnan(distance_matrix_metres), axis=1)
    )
    bad_indices = numpy.where(lifetime_fractions < min_lifetime_fraction)[0]
    distance_matrix_metres[bad_indices, ...] = LARGE_NUMBER

    mean_distances_metres = numpy.nanmean(distance_matrix_metres, axis=1)
    good_indices = numpy.where(mean_distances_metres <= max_distance_metres)[0]

    print((
        'Linked warning to {0:d} storms.  All distances (metres) printed below:'
        '\n{1:s}'
    ).format(
        len(good_indices), str(mean_distances_metres)
    ))

    return [unique_sec_id_strings[k] for k in good_indices]


def _write_linked_warnings(warning_table, output_file_name):
    """Writes linked warnings to Pickle file.

    :param warning_table: pandas DataFrame with the following columns.  Each row
        is one warning.
    warning_table.start_time_unix_sec: Start time.
    warning_table.end_time_unix_sec: End time.
    warning_table.polygon_object_latlng: Polygon (instance of
        `shapely.geometry.Polygon`) with lat-long coordinates of warning
        boundary.
    warning_table.linked_sec_id_strings: 1-D list of secondary ID strings for
        storms to which warning is linked.

    :param output_file_name: Path to output file.
    """

    file_system_utils.mkdir_recursive_if_necessary(file_name=output_file_name)
    warning_table.drop(WARNING_XY_POLYGON_KEY, axis=1, inplace=True)

    print('Writing results to: "{0:s}"...'.format(output_file_name))
    pickle_file_handle = open(output_file_name, 'wb')
    pickle.dump(warning_table, pickle_file_handle)
    pickle_file_handle.close()


def _run(input_warning_file_name, top_tracking_dir_name, spc_date_string,
         max_distance_metres, min_lifetime_fraction, output_warning_file_name):
    """Links each NWS tornado warning to nearest storm.

    This is effectively the main method.

    :param input_warning_file_name: See documentation at top of file.
    :param top_tracking_dir_name: Same.
    :param spc_date_string: Same.
    :param max_distance_metres: Same.
    :param min_lifetime_fraction: Same.
    :param output_warning_file_name: Same.
    """

    error_checking.assert_is_greater(max_distance_metres, 0.)
    error_checking.assert_is_greater(min_lifetime_fraction, 0.)
    error_checking.assert_is_leq(min_lifetime_fraction, 1.)

    print('Reading warnings from: "{0:s}"...'.format(input_warning_file_name))
    this_file_handle = open(input_warning_file_name, 'rb')
    warning_table = pickle.load(this_file_handle)
    this_file_handle.close()

    date_start_time_unix_sec = (
        time_conversion.get_start_of_spc_date(spc_date_string)
    )
    date_end_time_unix_sec = (
        time_conversion.get_end_of_spc_date(spc_date_string)
    )
    warning_table = warning_table.loc[
        (warning_table[WARNING_START_TIME_KEY] >= date_start_time_unix_sec) &
        (warning_table[WARNING_START_TIME_KEY] <= date_end_time_unix_sec)
    ]
    num_warnings = len(warning_table.index)

    print('Number of warnings beginning on SPC date "{0:s}" = {1:d}'.format(
        spc_date_string, num_warnings
    ))

    warning_polygon_objects_xy = [None] * num_warnings
    nested_array = warning_table[[
        WARNING_START_TIME_KEY, WARNING_START_TIME_KEY
    ]].values.tolist()

    warning_table = warning_table.assign(**{
        WARNING_XY_POLYGON_KEY: warning_polygon_objects_xy,
        LINKED_SECONDARY_IDS_KEY: nested_array
    })

    for k in range(num_warnings):
        warning_table[LINKED_SECONDARY_IDS_KEY].values[k] = []

        this_object_latlng = warning_table[WARNING_LATLNG_POLYGON_KEY].values[k]

        warning_table[WARNING_XY_POLYGON_KEY].values[k], _ = (
            polygons.project_latlng_to_xy(
                polygon_object_latlng=this_object_latlng,
                projection_object=PROJECTION_OBJECT)
        )

    tracking_file_names = []

    for i in [-1, 0, 1]:
        this_spc_date_string = time_conversion.time_to_spc_date_string(
            date_start_time_unix_sec + i * NUM_SECONDS_PER_DAY
        )

        # tracking_file_names += tracking_io.find_files_one_spc_date(
        #     top_tracking_dir_name=top_tracking_dir_name,
        #     tracking_scale_metres2=DUMMY_TRACKING_SCALE_METRES2,
        #     source_name=tracking_utils.SEGMOTION_NAME,
        #     spc_date_string=this_spc_date_string,
        #     raise_error_if_missing=i == 0
        # )[0]

        tracking_file_names += tracking_io.find_files_one_spc_date(
            top_tracking_dir_name=top_tracking_dir_name,
            tracking_scale_metres2=DUMMY_TRACKING_SCALE_METRES2,
            source_name=tracking_utils.SEGMOTION_NAME,
            spc_date_string=this_spc_date_string,
            raise_error_if_missing=False
        )[0]

    if len(tracking_file_names) == 0:
        _write_linked_warnings(
            warning_table=warning_table,
            output_file_name=output_warning_file_name
        )

        return

    print(SEPARATOR_STRING)
    storm_object_table = tracking_io.read_many_files(tracking_file_names)
    print(SEPARATOR_STRING)

    if len(storm_object_table.index) == 0:
        _write_linked_warnings(
            warning_table=warning_table,
            output_file_name=output_warning_file_name
        )

        return

    storm_object_table = linkage._project_storms_latlng_to_xy(
        storm_object_table=storm_object_table,
        projection_object=PROJECTION_OBJECT
    )

    for k in range(num_warnings):
        this_start_time_string = time_conversion.unix_sec_to_string(
            warning_table[WARNING_START_TIME_KEY].values[k],
            LOG_MESSAGE_TIME_FORMAT
        )

        this_end_time_string = time_conversion.unix_sec_to_string(
            warning_table[WARNING_END_TIME_KEY].values[k],
            LOG_MESSAGE_TIME_FORMAT
        )

        print('Attempting to link warning from {0:s} to {1:s}...'.format(
            this_start_time_string, this_end_time_string
        ))

        warning_table[LINKED_SECONDARY_IDS_KEY].values[k] = _link_one_warning(
            warning_table=warning_table.iloc[[k]],
            storm_object_table=copy.deepcopy(storm_object_table),
            max_distance_metres=max_distance_metres,
            min_lifetime_fraction=min_lifetime_fraction
        )

        print('\n')

    _write_linked_warnings(
        warning_table=warning_table,
        output_file_name=output_warning_file_name
    )


if __name__ == '__main__':
    INPUT_ARG_OBJECT = INPUT_ARG_PARSER.parse_args()

    _run(
        input_warning_file_name=getattr(
            INPUT_ARG_OBJECT, INPUT_WARNING_FILE_ARG_NAME
        ),
        top_tracking_dir_name=getattr(INPUT_ARG_OBJECT, TRACKING_DIR_ARG_NAME),
        spc_date_string=getattr(INPUT_ARG_OBJECT, SPC_DATE_ARG_NAME),
        max_distance_metres=getattr(INPUT_ARG_OBJECT, MAX_DISTANCE_ARG_NAME),
        min_lifetime_fraction=getattr(
            INPUT_ARG_OBJECT, MIN_LIFETIME_FRACTION_ARG_NAME
        ),
        output_warning_file_name=getattr(
            INPUT_ARG_OBJECT, OUTPUT_WARNING_FILE_ARG_NAME
        )
    )
