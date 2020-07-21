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

import atexit
import datetime
import os
import re
import shutil
import tempfile
from typing import Iterator, Tuple, List, Optional, Dict

import cdsapi
import dateutil.parser
import dateutil.relativedelta
import dateutil.rrule
import xarray as xr
import xcube.core.normalize
from xcube.core.store import DataDescriptor
from xcube.core.store import DataOpener
from xcube.core.store import DataStore
from xcube.core.store import DataStoreError
from xcube.core.store import TYPE_ID_DATASET
from xcube.util.jsonschema import JsonBooleanSchema
from xcube.util.jsonschema import JsonIntegerSchema
from xcube.util.jsonschema import JsonObjectSchema

from xcube_cds.constants import CDS_DATA_OPENER_ID
from xcube_cds.constants import DEFAULT_NUM_RETRIES

from xcube_cds.datasets.reanalysis_era5 import ERA5DatasetHandler


class CDSDataOpener(DataOpener):
    """A data opener for the Copernicus Climate Data Store"""

    def __init__(self, normalize_names: Optional[bool] = False):
        self._normalize_names = normalize_names
        self._create_temporary_directory()
        self._handler_registry = {}
        self._register_dataset_handler(ERA5DatasetHandler())

    def _register_dataset_handler(self, handler):
        for data_id in handler.get_supported_data_ids():
            self._handler_registry[data_id] = handler

    def _create_temporary_directory(self):
        # Create a temporary directory to hold downloaded NetCDF files and
        # a hook to delete it when the interpreter exits. xarray.open reads
        # data lazily so we can't just delete the file after returning the
        # Dataset. We could also use weakref hooks to delete individual files
        # when the corresponding object is garbage collected, but even then
        # the directory is useful to group the files and offer an extra
        # assurance that they will be deleted.
        tempdir = tempfile.mkdtemp()

        def delete_tempdir():
            shutil.rmtree(tempdir, ignore_errors=True)

        atexit.register(delete_tempdir)
        self._tempdir = tempdir

    ###########################################################################
    # DataOpener implementation

    def get_open_data_params_schema(self, data_id: Optional[str] = None) -> \
            JsonObjectSchema:

        self._validate_data_id(data_id, allow_none=True)

        # TODO: define a broad default schema here for the case where
        # data_id==None. If data_id is supplied, its values can be
        # overwritten.

        if data_id is None:
            raise NotImplementedError("data_id==None not implemented yet.")
        else:
            return _handler_registry[data_id].\
                get_open_data_params_schema(data_id)

    def open_data(self, data_id: str, **open_params) -> xr.Dataset:
        schema = self.get_open_data_params_schema(data_id)
        schema.validate_instance(open_params)

        client = cdsapi.Client()

        # We can't generate a safe unique filename (since the file is created
        # by client.retrieve, so name generation and file creation won't be
        # atomic). Instead we atomically create a subdirectory of the temporary
        # directory for the single file.
        subdir = tempfile.mkdtemp(dir=self._tempdir)
        file_path = os.path.join(subdir, 'data.nc')
        dataset_name, cds_api_params = \
            CDSDataOpener._transform_params(open_params, data_id)

        # This call returns a Result object, which at present we make
        # no use of.
        client.retrieve(dataset_name, cds_api_params, file_path)

        # decode_cf is the default, but it's clearer to make it explicit.
        dataset = xr.open_dataset(file_path, decode_cf=True)

        # The API doesn't close the session automatically, so we need to
        # do it explicitly here to avoid leaving an open socket.
        client.session.close()

        dataset = dataset.rename_dims({
            'longitude': 'lon',
            'latitude': 'lat'
        })
        dataset = dataset.rename_vars({'longitude': 'lon', 'latitude': 'lat'})
        dataset.transpose('time', ..., 'lat', 'lon')
        dataset.coords['time'].attrs['standard_name'] = 'time'
        dataset.coords['lat'].attrs['standard_name'] = 'latitude'
        dataset.coords['lon'].attrs['standard_name'] = 'longitude'

        # Correct units not entirely clear: cubespec document says
        # degrees_north / degrees_east for WGS84 Schema, but SH Plugin
        # had decimal_degrees.
        dataset.coords['lat'].attrs['units'] = 'degrees_north'
        dataset.coords['lon'].attrs['units'] = 'degrees_east'

        # TODO: Temporal coordinate variables MUST have units, standard_name,
        # and any others. standard_name MUST be "time", units MUST have
        # format "<deltatime> since <datetime>", where datetime must have
        # ISO-format.

        dataset = xcube.core.normalize.normalize_dataset(dataset)

        if self._normalize_names:
            rename_dict = {}
            for name in dataset.data_vars.keys():
                normalized_name = re.sub(r'\W|^(?=\d)', '_', name)
                if name != normalized_name:
                    rename_dict[name] = normalized_name
            dataset_renamed = dataset.rename_vars(rename_dict)
            return dataset_renamed
        else:
            return dataset

    @staticmethod
    def _transform_params(plugin_params, data_id):
        """Transform supplied parameters to CDS API format.

        :param plugin_params: parameters in form expected by this plugin
        :return: parameters in form expected by the CDS API
        """

        dataset_name, product_type = \
            data_id.split(':') if ':' in data_id else (data_id, None)

        # We need to split out the bounding box co-ordinates to re-order them.
        x1, y1, x2, y2 = plugin_params['bbox']

        # Translate our parameters (excluding time parameters) to the CDS API
        # scheme.
        resolution = plugin_params['spatial_res']
        params_combined = {
            'variable': plugin_params['variable_names'],
            # For the ERA5 dataset, we need to crop the area by half a
            # cell-width. ERA5 data are points, but xcube treats them as
            # cell centres. The bounds of a grid of cells are half a cell-width
            # outside the bounds of a grid of points, so we have to crop each
            # edge by half a cell-width to end up with the requested bounds.
            # See https://confluence.ecmwf.int/display/CKB/ERA5%3A+What+is+the+spatial+reference#ERA5:Whatisthespatialreference-Visualisationofregularlat/londata
            'area': [y2 - resolution / 2,
                     x1 + resolution / 2,
                     y1 + resolution / 2,
                     x2 - resolution / 2],
            # Note: the "grid" parameter is not exposed via the web interface,
            # but is described at
            # https://confluence.ecmwf.int/display/CKB/ERA5%3A+Web+API+to+CDS+API
            'grid': [resolution, resolution],
            'format': 'netcdf'
        }

        if product_type is not None:
            params_combined['product_type'] = product_type

        # Convert the time range specification to the nearest equivalent
        # in the CDS "orthogonal time units" scheme.
        time_params_from_range = CDSDataOpener._transform_time_params(
            CDSDataOpener._convert_time_range(plugin_params['time_range']))
        params_combined.update(time_params_from_range)

        # If any of the "years", "months", "days", and "hours" parameters
        # were passed, they override the time specifications above.
        time_params_explicit = \
            CDSDataOpener._transform_time_params(plugin_params)
        params_combined.update(time_params_explicit)

        # Transform singleton list values into their single members, as
        # required by the CDS API.
        desingletonned = {
            k: (v[0] if isinstance(v, list) and len(v) == 1 else v)
            for k, v in params_combined.items()}

        return dataset_name, desingletonned

    @staticmethod
    def _transform_time_params(params: Dict):
        return {
            k1: v1 for k1, v1 in [CDSDataOpener._transform_time_param(k0, v0)
                                  for k0, v0 in params.items()]
            if k1 is not None}

    @staticmethod
    def _transform_time_param(key, value):
        if key == 'hours':
            return 'time', list(map(lambda x: f'{x:02d}:00', value))
        if key == 'days':
            return 'day', list(map(lambda x: f'{x:02d}', value))
        elif key == 'months':
            return 'month', list(map(lambda x: f'{x:02d}', value))
        elif key == 'years':
            return 'year', list(map(lambda x: f'{x:04d}', value))
        else:
            return None, None

    @staticmethod
    def _convert_time_range(time_range: List[str]) -> \
            Dict[str, List[int]]:
        """Convert a time range to a CDS-style time specification.

        This method converts a time range specification (i.e. a straightforward
        pair of "start time" and "end time") into the closest corresponding
        specification for CDS datasets such as ERA5 (which allow orthogonal
        selection of subsets of years, months, days, and hours). "Closest"
        here means the narrowest selection which will cover the entire
        requested time range, although it will often cover significantly more.
        For example, the range 2000-12-31 to 2002-01-02 would be translated
        to a request for all of 2000-2002, since every hour, day, and month
        must be selected.

        :param time_range: a length-2 list of ISO-8601 date/time strings
        :return: a dictionary with keys 'hours', 'days', 'months', and
            'years', and values which are lists of ints
        """

        if len(time_range) != 2:
            raise ValueError(f'time_range must have a length of 2, '
                             'not {len(time_range)}.')

        time0 = dateutil.parser.isoparse(time_range[0])
        time1 = datetime.datetime.now() if time_range[1] is None \
            else dateutil.parser.isoparse(time_range[1])

        # We use datetime's recurrence rule features to enumerate the
        # hour / day / month numbers which intersect with the selected time
        # range.

        hour0 = datetime.datetime(time0.year, time0.month, time0.day,
                                  time0.hour, 0)
        hour1 = datetime.datetime(time1.year, time1.month, time1.day,
                                  time1.hour, 59)
        hours = [dt.hour for dt in dateutil.rrule.rrule(
            freq=dateutil.rrule.HOURLY, count=24,
            dtstart=hour0, until=hour1)]
        hours.sort()

        day0 = datetime.datetime(time0.year, time0.month, time0.day, 0, 1)
        day1 = datetime.datetime(time1.year, time1.month, time1.day, 23, 59)
        days = [dt.day for dt in dateutil.rrule.rrule(
            freq=dateutil.rrule.DAILY, count=31, dtstart=day0, until=day1)]
        days = sorted(set(days))

        month0 = datetime.datetime(time0.year, time0.month, 1)
        month1 = datetime.datetime(time1.year, time1.month, 28)
        months = [dt.month for dt in dateutil.rrule.rrule(
            freq=dateutil.rrule.MONTHLY, count=12,
            dtstart=month0, until=month1)]
        months.sort()

        years = list(range(time0.year, time1.year + 1))

        return dict(hours=hours,
                    days=days,
                    months=months, years=years)

    def _validate_data_id(self, data_id, allow_none=False):
        if (data_id is None) and allow_none:
            return
        if data_id not in _handler_registry:
            raise ValueError(f'Unknown data id "{data_id}"')


class CDSDataStore(CDSDataOpener, DataStore):

    def __init__(self,
                 num_retries: Optional[int] = DEFAULT_NUM_RETRIES,
                 **kwargs):
        super().__init__(**kwargs)
        self.num_retries = num_retries

    ###########################################################################
    # DataStore implementation

    @classmethod
    def get_data_store_params_schema(cls) -> JsonObjectSchema:
        params = dict(
            normalize_names=JsonBooleanSchema(default=False)
        )

        # For now, let CDS API use defaults or environment variables for
        # most parameters.
        cds_params = dict(
            num_retries=JsonIntegerSchema(default=DEFAULT_NUM_RETRIES,
                                          minimum=0),
        )

        params.update(cds_params)
        return JsonObjectSchema(
            properties=params,
            required=None,
            additional_properties=False
        )

    @classmethod
    def get_type_ids(cls) -> Tuple[str, ...]:
        return TYPE_ID_DATASET,

    def get_data_ids(self, type_id: Optional[str] = None) -> \
            Iterator[Tuple[str, Optional[str]]]:
        self._assert_valid_type_id(type_id)
        return iter((data_id,
                     _handler_registry[data_id].
                     get_human_readable_data_id[data_id])
                    for data_id in _handler_registry)

    def has_data(self, data_id: str) -> bool:
        return data_id in _handler_registry

    def describe_data(self, data_id: str) -> DataDescriptor:
        self._validate_data_id(data_id)
        return _handler_registry[data_id].describe_data(data_id)

    # noinspection PyTypeChecker
    def search_data(self, type_id: Optional[str] = None, **search_params) -> \
            Iterator[DataDescriptor]:
        self._assert_valid_type_id(type_id)
        raise NotImplementedError()

    def get_data_opener_ids(self, data_id: Optional[str] = None,
                            type_id: Optional[str] = None) -> \
            Tuple[str, ...]:
        self._assert_valid_type_id(type_id)
        self._assert_valid_opener_id(data_id)
        return CDS_DATA_OPENER_ID,

    def get_open_data_params_schema(self, data_id: Optional[str] = None,
                                    opener_id: Optional[str] = None) -> \
            JsonObjectSchema:
        self._assert_valid_opener_id(opener_id)
        self._validate_data_id(data_id)
        return super().get_open_data_params_schema(data_id)

    def open_data(self, data_id: str, opener_id: Optional[str] = None,
                  **open_params) -> xr.Dataset:
        self._assert_valid_opener_id(opener_id)
        self._validate_data_id(data_id)
        return super().open_data(data_id, **open_params)

    ###########################################################################
    # Implementation helpers

    @staticmethod
    def _assert_valid_type_id(type_id):
        if type_id is not None and type_id != TYPE_ID_DATASET:
            raise DataStoreError(
                f'Data type identifier must be "{TYPE_ID_DATASET}", '
                f'but got "{type_id}"')

    @staticmethod
    def _assert_valid_opener_id(opener_id):
        if opener_id is not None and opener_id != CDS_DATA_OPENER_ID:
            raise DataStoreError(
                f'Data opener identifier must be "{CDS_DATA_OPENER_ID}"'
                f'but got "{opener_id}"')
