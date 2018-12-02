"""Runs echo-top-based storm-tracking."""

import argparse
from gewittergefahr.gg_io import myrorss_io
from gewittergefahr.gg_utils import radar_utils
from gewittergefahr.gg_utils import time_conversion
from gewittergefahr.gg_utils import echo_top_tracking

SEPARATOR_STRING = '\n\n' + '*' * 50 + '\n\n'

NATIVE_ECHO_TOP_FIELD_NAMES = [
    radar_utils.ECHO_TOP_18DBZ_NAME, radar_utils.ECHO_TOP_50DBZ_NAME
]

ECHO_TOP_FIELD_ARG_NAME = 'echo_top_field_name'
TARRED_RADAR_DIR_ARG_NAME = 'input_radar_dir_name_tarred'
RADAR_DIR_ARG_NAME = 'input_radar_dir_name'
ECHO_CLASSIFN_DIR_ARG_NAME = 'input_echo_classifn_dir_name'
MIN_ECHO_TOP_ARG_NAME = 'min_echo_top_km_asl'
MIN_GRID_CELLS_ARG_NAME = 'min_grid_cells_in_polygon'
OUTPUT_DIR_ARG_NAME = 'output_tracking_dir_name'
FIRST_SPC_DATE_ARG_NAME = 'first_spc_date_string'
LAST_SPC_DATE_ARG_NAME = 'last_spc_date_string'

ECHO_TOP_FIELD_HELP_STRING = (
    'Tracking will be based on this field.  Must be in the following list.'
    '\n{0:s}'
).format(str(radar_utils.ECHO_TOP_NAMES))

TARRED_RADAR_DIR_HELP_STRING = (
    '[used only if {0:s} = "{1:s}" or "{2:s}"] Name of top-level directory with'
    ' tarred MYRORSS files.  These files will be untarred before processing, to'
    ' the directory `{3:s}`, and the untarred files will be deleted after '
    'processing.'
).format(ECHO_TOP_FIELD_ARG_NAME, NATIVE_ECHO_TOP_FIELD_NAMES[0],
         NATIVE_ECHO_TOP_FIELD_NAMES[1], RADAR_DIR_ARG_NAME)

RADAR_DIR_HELP_STRING = (
    'Name of top-level radar directory.  Files therein will be found by '
    '`echo_top_tracking._find_input_radar_files`.')

ECHO_CLASSIFN_DIR_HELP_STRING = (
    'Name of top-level directory with echo classifications.  If empty (""), '
    'echo classifications will not be used.  If non-empty, files therein will '
    'be found by `echo_classification.find_classification_file` and read by '
    '`echo_classification.read_classifications` and tracking will be run only '
    'on convective pixels.')

MIN_ECHO_TOP_HELP_STRING = (
    'Minimum echo top (km above sea level).  Only maxima with '
    '`{0:s}` >= `{1:s}` will be considered storm objects.  Smaller maxima will '
    'be thrown out.'
).format(ECHO_TOP_FIELD_ARG_NAME, MIN_ECHO_TOP_ARG_NAME)

MIN_GRID_CELLS_HELP_STRING = (
    'Minimum storm-object size.  Smaller objects will be thrown out.')

OUTPUT_DIR_HELP_STRING = (
    'Name of top-level output directory.  Files will be written here by '
    '`echo_top_tracking._write_storm_objects`.')

SPC_DATE_HELP_STRING = (
    'SPC date (format "yyyymmdd").  This script will track storms in the period'
    ' `{0:s}`...`{1:s}`.'
).format(FIRST_SPC_DATE_ARG_NAME, LAST_SPC_DATE_ARG_NAME)

DEFAULT_TARRED_RADAR_DIR_NAME = '/condo/swatcommon/common/myrorss'

INPUT_ARG_PARSER = argparse.ArgumentParser()
INPUT_ARG_PARSER.add_argument(
    '--' + ECHO_TOP_FIELD_ARG_NAME, type=str, required=False,
    default=radar_utils.ECHO_TOP_40DBZ_NAME, help=ECHO_TOP_FIELD_HELP_STRING)

INPUT_ARG_PARSER.add_argument(
    '--' + TARRED_RADAR_DIR_ARG_NAME, type=str, required=False,
    default=DEFAULT_TARRED_RADAR_DIR_NAME, help=TARRED_RADAR_DIR_HELP_STRING)

INPUT_ARG_PARSER.add_argument(
    '--' + RADAR_DIR_ARG_NAME, type=str, required=True,
    help=RADAR_DIR_HELP_STRING)

INPUT_ARG_PARSER.add_argument(
    '--' + ECHO_CLASSIFN_DIR_ARG_NAME, type=str, required=False, default='',
    help=ECHO_CLASSIFN_DIR_HELP_STRING)

INPUT_ARG_PARSER.add_argument(
    '--' + MIN_ECHO_TOP_ARG_NAME, type=float, required=False, default=4.,
    help=MIN_ECHO_TOP_HELP_STRING)

INPUT_ARG_PARSER.add_argument(
    '--' + MIN_GRID_CELLS_ARG_NAME, type=int, required=False, default=0,
    help=MIN_GRID_CELLS_HELP_STRING)

INPUT_ARG_PARSER.add_argument(
    '--' + OUTPUT_DIR_ARG_NAME, type=str, required=True,
    help=OUTPUT_DIR_HELP_STRING)

INPUT_ARG_PARSER.add_argument(
    '--' + FIRST_SPC_DATE_ARG_NAME, type=str, required=True,
    help=SPC_DATE_HELP_STRING)

INPUT_ARG_PARSER.add_argument(
    '--' + LAST_SPC_DATE_ARG_NAME, type=str, required=True,
    help=SPC_DATE_HELP_STRING)


def _run(echo_top_field_name, top_radar_dir_name_tarred, top_radar_dir_name,
         top_echo_classifn_dir_name, min_echo_top_km_asl,
         min_grid_cells_in_polygon, top_output_dir_name, first_spc_date_string,
         last_spc_date_string):
    """Runs echo-top-based storm-tracking.

    This is effectively the main method.

    :param echo_top_field_name: See documentation at top of file.
    :param top_radar_dir_name_tarred: Same.
    :param top_radar_dir_name: Same.
    :param top_echo_classifn_dir_name: Same.
    :param min_echo_top_km_asl: Same.
    :param min_grid_cells_in_polygon: Same.
    :param top_output_dir_name: Same.
    :param first_spc_date_string: Same.
    :param last_spc_date_string: Same.
    """

    spc_date_strings = time_conversion.get_spc_dates_in_range(
        first_spc_date_string=first_spc_date_string,
        last_spc_date_string=last_spc_date_string)

    for this_spc_date_string in spc_date_strings:
        this_tar_file_name = '{0:s}/{1:s}/{2:s}.tar'.format(
            top_radar_dir_name_tarred, this_spc_date_string[:4],
            this_spc_date_string)

        myrorss_io.unzip_1day_tar_file(
            tar_file_name=this_tar_file_name, field_names=[echo_top_field_name],
            spc_date_string=this_spc_date_string,
            top_target_directory_name=top_radar_dir_name)
        print SEPARATOR_STRING

    if top_echo_classifn_dir_name in ['', 'None']:
        top_echo_classifn_dir_name = None

    echo_top_tracking.run_tracking(
        top_radar_dir_name=top_radar_dir_name,
        top_output_dir_name=top_output_dir_name,
        first_spc_date_string=first_spc_date_string,
        last_spc_date_string=last_spc_date_string,
        echo_top_field_name=echo_top_field_name,
        top_echo_classifn_dir_name=top_echo_classifn_dir_name,
        min_echo_top_height_km_asl=min_echo_top_km_asl,
        min_grid_cells_in_polygon=min_grid_cells_in_polygon)
    print SEPARATOR_STRING

    for this_spc_date_string in spc_date_strings:
        myrorss_io.remove_unzipped_data_1day(
            spc_date_string=this_spc_date_string,
            top_directory_name=top_radar_dir_name,
            field_names=[echo_top_field_name])


if __name__ == '__main__':
    INPUT_ARG_OBJECT = INPUT_ARG_PARSER.parse_args()

    _run(
        echo_top_field_name=getattr(INPUT_ARG_OBJECT, ECHO_TOP_FIELD_ARG_NAME),
        top_radar_dir_name_tarred=getattr(
            INPUT_ARG_OBJECT, TARRED_RADAR_DIR_ARG_NAME),
        top_radar_dir_name=getattr(INPUT_ARG_OBJECT, RADAR_DIR_ARG_NAME),
        top_echo_classifn_dir_name=getattr(
            INPUT_ARG_OBJECT, ECHO_CLASSIFN_DIR_ARG_NAME),
        min_echo_top_km_asl=float(
            getattr(INPUT_ARG_OBJECT, MIN_ECHO_TOP_ARG_NAME)),
        min_grid_cells_in_polygon=getattr(
            INPUT_ARG_OBJECT, MIN_GRID_CELLS_ARG_NAME),
        top_output_dir_name=getattr(INPUT_ARG_OBJECT, OUTPUT_DIR_ARG_NAME),
        first_spc_date_string=getattr(
            INPUT_ARG_OBJECT, FIRST_SPC_DATE_ARG_NAME),
        last_spc_date_string=getattr(INPUT_ARG_OBJECT, LAST_SPC_DATE_ARG_NAME)
    )
