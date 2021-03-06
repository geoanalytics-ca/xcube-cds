# MIT License
#
# Copyright (c) 2020 Brockmann Consult GmbH
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
import tarfile
from typing import Any
from typing import Dict
from typing import List
from typing import Tuple

import xarray as xr
from xcube.core.store import DataDescriptor
from xcube.core.store import DatasetDescriptor
from xcube.core.store import VariableDescriptor
from xcube.util.jsonschema import JsonArraySchema
from xcube.util.jsonschema import JsonDateSchema
from xcube.util.jsonschema import JsonNumberSchema
from xcube.util.jsonschema import JsonObjectSchema
from xcube.util.jsonschema import JsonStringSchema

from xcube_cds.store import CDSDatasetHandler


class SoilMoistureHandler(CDSDatasetHandler):

    def __init__(self):
        self._data_id_map = {
            'satellite-soil-moisture:saturation:daily':
                'Soil moisture (saturation, daily)',
            'satellite-soil-moisture:saturation:10-day':
                'Soil moisture (saturation, 10-day)',
            'satellite-soil-moisture:saturation:monthly':
                'Soil moisture (saturation, monthly)',
            'satellite-soil-moisture:volumetric:daily':
                'Soil moisture (volumetric, daily)',
            'satellite-soil-moisture:volumetric:10-day':
                'Soil moisture (volumetric, 10-day)',
            'satellite-soil-moisture:volumetric:monthly':
                'Soil moisture (volumetric, monthly)',
        }
        self._var_map = {
            'saturation': (['soil_moisture_saturation'], ['active']),
            'volumetric': (['volumetric_surface_soil_moisture'],
                           ['combined_passive_and_active', 'passive'])
        }
        self._aggregation_map = {'daily': '1D',
                                 '10-day': '10D',
                                 'monthly': '1M'}

    def transform_params(self, opener_params, data_id: str) -> \
            Tuple[str, Dict[str, Any]]:
        # We don't need to check the argument format, since CDSDataStore does
        # this for us. We can also ignore the dataset ID (constant).
        _, variable_spec, aggregation = data_id.split(':')

        variables = opener_params['variable_names']

        # We ignore any aggregation period passed in the opener parameters
        # (since it's in any case optional and limited to a single possible
        # value), and instead take it directly from the third part of the
        # data_id.
        cds_aggregation_specifier = {
            'daily': 'day_average',
            '1-day': '10_day_average',
            'monthly': 'month_average'}[aggregation]

        cds_params = dict(
            variable=variables,
            type_of_sensor=opener_params['type_of_sensor'],
            time_aggregation=cds_aggregation_specifier,
            type_of_record=opener_params['type_of_record'],
            version=opener_params['version'],
            format='tgz'
        )

        time_selectors = self.transform_time_params(
            self.convert_time_range(opener_params['time_range']))
        cds_params.update(time_selectors)

        # Transform singleton list values into their single members, as
        # required by the CDS API.
        unwrapped = self.unwrap_singleton_values(cds_params)

        return 'satellite-soil-moisture', unwrapped

    def read_file(self, dataset_name: str, cds_api_params: Dict,
                  file_path: str, temp_dir: str):
        # Unpack the .tar.gz into the temporary directory.
        with tarfile.open(file_path) as tgz_file:
            tgz_file.extractall(path=temp_dir)

        paths = [os.path.join(temp_dir, filename) for filename in
                 next(os.walk(temp_dir))[2]]

        # I'm not sure if xr.open_mfdataset calls through to
        # netCDF4.MFDataset. If it does, note that the latter supports
        # NetCDF4 Classic, but *not* full NetCDF4 -- however, in this case
        # it's OK because the Product User Guide (C3S_312a_Lot7_EODC_2016SC1,
        # §1, p. 12) states that the data are in Classic format,
        # and inspection of some downloaded files confirms it.
        ds = xr.open_mfdataset(paths, combine='by_coords')
        ds.attrs.update(self.combine_netcdf_time_limits(paths))
        return ds

    def get_supported_data_ids(self) -> List[str]:
        return list(self._data_id_map)

    def get_open_data_params_schema(self, data_id: str) -> JsonObjectSchema:
        _, variable_spec, aggregation = data_id.split(':')
        variables = self._var_map[variable_spec][0]
        sensors = self._var_map[variable_spec][1]
        params = dict(
            dataset_name=JsonStringSchema(min_length=1,
                                          enum=self.get_supported_data_ids()),
            # The only allowed variable is already determined by the
            # data_id, so this schema forces an array containing only that
            # variable.
            variable_names=JsonArraySchema(
                items=(JsonStringSchema(
                    min_length=0,
                    enum=variables,
                    default=variables[0])),
                unique_items=True
            ),
            # Source for CRS information: §6.5 of
            # https://www.esa-soilmoisture-cci.org/sites/default/files/documents/CCI2_Soil_Moisture_D3.3.1_Product_Users_Guide%201.2.pdf
            crs=JsonStringSchema(nullable=True, default='WGS84',
                                 enum=[None, 'WGS84']),
            # W, S, E, N (will be converted to N, W, S, E).
            # For the soil moisture dataset, all data is global and no
            # geographic subsetting is possible, so the values are fixed
            # (i.e. minimum == maximum for every limit).
            bbox=JsonArraySchema(items=(
                JsonNumberSchema(minimum=-180, maximum=-180),
                JsonNumberSchema(minimum=-90, maximum=-90),
                JsonNumberSchema(minimum=180, maximum=180),
                JsonNumberSchema(minimum=90, maximum=90))),
            # Like the bounding box, the spatial resolution is fixed.
            spatial_res=JsonNumberSchema(minimum=0.25,
                                         maximum=0.25,
                                         default=0.25),
            time_range=JsonDateSchema.new_range(),
            time_period=JsonStringSchema(
                enum=[self._aggregation_map[aggregation]]),
            # Non-standard parameters start here. There are complex
            # interdependencies between allowed values for these and for
            # the date specifiers, which can't be represented in JSON Schema.
            # The best we can do is to make them all available, set sensible
            # defaults, and trust that the user knows what they're requesting.
            type_of_sensor=JsonStringSchema(
                enum=sensors,
                default=sensors[0],
                title='Type of sensor',
                description=(
                    'Passive sensors measure reflected sunlight. '
                    'Active sensors have their own source of illumination.'
                )),
            type_of_record=JsonStringSchema(
                enum=['cdr', 'icdr'],
                title='Type of record',
                description=(
                    'When dealing with satellite data it is common to '
                    'encounter references to Climate Data Records (CDR) and '
                    'interim-CDR (ICDR). For this dataset, both the ICDR and '
                    'CDR parts of each product were generated using the same '
                    'software and algorithms. The CDR is intended to have '
                    'sufficient length, consistency, and continuity to detect '
                    'climate variability and change. The ICDR provides a '
                    'short-delay access to current data where consistency with '
                    'the CDR baseline is expected but was not extensively '
                    'checked.'),
                default='cdr'),
            version=JsonStringSchema(
                enum=['v201706.0.0', 'v201812.0.0', 'v201812.0.1',
                      'v201912.0.0'],
                title='Data version',
                description=(
                    'Format: vMajor.Minor.Run, e.g. "v201706.0.0". The Major '
                    'number usually represents the year (YYYY) and month (MM) '
                    'of date. The initial value for Minor is zero, and will '
                    'increment when updating the file. If there is a need – '
                    'e.g. because of technical issues – to replace a file '
                    'which has already been made public, the Run number of '
                    'the replacement file shifts to the next increment. The '
                    'initial Run number is zero.'),
                default='v201912.0.0')
        )
        required = [
            'variable_names',
            'time_range',
        ]
        return JsonObjectSchema(
            properties=dict(
                **params,
            ),
            required=required
        )

    def get_human_readable_data_id(self, data_id: str):
        return self._data_id_map[data_id]

    def describe_data(self, data_id: str) -> DataDescriptor:
        _, variable_spec, aggregation = data_id.split(':')

        sm_attrs = dict(
            saturation=('percent', 'Percent of Saturation Soil Moisture'),
            volumetric=('m3 m-3', 'Volumetric Soil Moisture'))[variable_spec]

        descriptors_common = [
            VariableDescriptor(
                name='sensor',
                dtype='int16',
                dims=('time', 'lat', 'lon'),
                attrs={'long_name': 'Sensor'}
            ),
            VariableDescriptor(
                name='freqbandID',
                dtype='int16',
                dims=('time', 'lat', 'lon'),
                attrs={'long_name': 'Frequency Band Identification'}
            ),
            VariableDescriptor(
                name='sm',
                dtype='float32',
                dims=('time', 'lat', 'lon'),
                attrs={'units': sm_attrs[0],
                       'long_name': sm_attrs[1]}
            ),
        ]

        descriptors_daily = [
            VariableDescriptor(
                # The product user guide claims that sm_uncertainty is
                # available for all three aggregation periods, but in practice
                # it only seems to be present in the daily data.
                name='sm_uncertainty',
                dtype='float32',
                dims=('time', 'lat', 'lon'),
                attrs={'units': sm_attrs[0],
                       'long_name': sm_attrs[1] + ' Uncertainty'}
            ),
            VariableDescriptor(
                name='t0',
                dtype='float64',
                dims=('time', 'lat', 'lon'),
                attrs={'units': 'days since 1970-01-01 00:00:00 UTC',
                       'long_name': 'Observation Timestamp'}
            ),
            VariableDescriptor(
                name='dnflag',
                dtype='int8',
                dims=('time', 'lat', 'lon'),
                attrs={'long_name': 'Day / Night Flag'}
            ),
            VariableDescriptor(
                name='flag',
                dtype='int8',
                dims=('time', 'lat', 'lon'),
                attrs={'long_name': 'Flag'}
            ),
            VariableDescriptor(
                name='mode',
                dtype='int8',
                dims=('time', 'lat', 'lon'),
                # Note: the product user guide gives the long name as
                # 'Satellite Mode' with one space, but the long name in the
                # actual NetCDF files has two spaces.
                attrs={'long_name': 'Satellite  Mode'}
            ),

        ]
        descriptors_aggregated = [
            VariableDescriptor(
                name='nobs',
                dtype='int16',
                dims=('time', 'lat', 'lon'),
                attrs={'long_name': 'Number of valid observation'}
            ),

        ]

        return DatasetDescriptor(
            data_id=data_id,
            data_vars=(descriptors_common +
                       (descriptors_daily if aggregation == 'daily'
                        else descriptors_aggregated)),
            crs='WGS84',
            bbox=(-180, -90, 180, 90),
            spatial_res=0.25,
            time_range=('1978-11-01', None),
            time_period=self._aggregation_map[aggregation],
            open_params_schema=self.get_open_data_params_schema(data_id)
        )
