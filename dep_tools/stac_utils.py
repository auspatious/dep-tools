from datetime import datetime
import json
from pathlib import Path

import numpy as np
from pandas import DataFrame
from pystac import Asset, Item, MediaType
import rasterio
from rio_stac.stac import create_stac_item, get_raster_info
from urlpath import URL
from xarray import DataArray, Dataset


from .aws import object_exists
from .namers import DepItemPath, S3ItemPath
from .processors import Processor


class StacCreator(Processor):
    def __init__(
        self,
        itempath: DepItemPath,
        remote: bool = True,
        collection_url_root: str = "https://stac.staging.digitalearthpacific.io/collections",
        aws_region: str = "us-west-2",
        make_hrefs_https: bool = True,
        **kwargs,
    ):
        self._itempath = itempath
        self._remote = remote
        self._collection_url_root = collection_url_root
        self._aws_region = aws_region
        self._make_hrefs_https = make_hrefs_https
        self._kwargs = kwargs

    def process(
        self,
        data: DataArray | Dataset,
        item_id: str,
    ) -> Item | str:
        return get_stac_item(
            itempath=self._itempath,
            item_id=item_id,
            data=data,
            remote=self._remote,
            collection_url_root=self._collection_url_root,
            aws_region=self._aws_region,
            make_hrefs_https=self._make_hrefs_https,
            **self._kwargs,
        )


def get_stac_item(
    itempath: DepItemPath,
    item_id: str,
    data: DataArray | Dataset,
    remote: bool = True,
    collection_url_root: str = "https://stac.staging.digitalearthpacific.org/collections",
    aws_region: str = "us-west-2",
    make_hrefs_https: bool = True,
    **kwargs,
) -> Item | str:
    prefix = Path("./")
    # Remote means not local
    # TODO: neaten local file writing up
    if remote:
        # Or, isinstance(itempath, S3ItemPath)
        if hasattr(itempath, "bucket"):
            # Writing to S3
            if make_hrefs_https:
                # E.g., https://dep-public-prod.s3.us-west-2.amazonaws.com/
                prefix = URL(
                    f"https://{getattr(itempath, 'bucket')}.s3.{aws_region}.amazonaws.com"
                )
            else:
                # E.g., s3://dep-public-prod/
                prefix = URL(f"s3://{getattr(itempath, 'bucket')}")
        else:
            # Default to Azure
            prefix = URL("https://deppcpublicstorage.blob.core.windows.net/output")

    properties = {}
    if "stac_properties" in data.attrs:
        properties = (
            json.loads(data.attrs["stac_properties"].replace("'", '"'))
            if isinstance(data.attrs["stac_properties"], str)
            else data.attrs["stac_properties"]
        )

    paths = [itempath.path(item_id, variable) for variable in data]

    assets = {}
    for variable, path in zip(data, paths):
        raster_info = {}
        full_path = str(prefix / path)
        if "with_raster" in kwargs.keys() and kwargs["with_raster"]:
            with rasterio.open(full_path) as src_dst:
                raster_info = {"raster:bands": get_raster_info(src_dst, max_size=1024)}

        assets[variable] = Asset(
            media_type=MediaType.COG,
            href=full_path,
            roles=["data"],
            extra_fields={**raster_info},
        )
    stac_id = itempath.basename(item_id)
    collection = itempath.item_prefix
    collection_url = f"{collection_url_root}/{collection}"

    input_datetime = properties.get("datetime", None)
    if input_datetime is not None:
        format_string = (
            "%Y-%m-%dT%H:%M:%S.%fZ" if "." in input_datetime else "%Y-%m-%dT%H:%M:%SZ"
        )
        input_datetime = datetime.strptime(input_datetime, format_string)

    item = create_stac_item(
        str(prefix / paths[0]),
        id=stac_id,
        input_datetime=input_datetime,
        assets=assets,
        with_proj=True,
        properties=properties,
        collection_url=collection_url,
        collection=collection,
        **kwargs,
    )

    stac_url = str(prefix / itempath.stac_path(item_id))
    item.set_self_href(stac_url)

    return item


def write_stac_local(item: Item, stac_path: str) -> None:
    with open(stac_path, "w") as f:
        json.dump(item.to_dict(), f, indent=4)


def existing_stac_items(possible_ids: list, itempath: S3ItemPath) -> list:
    """Returns only those ids which have an existing stac item."""
    return [
        id
        for id in possible_ids
        if object_exists(itempath.bucket, itempath.stac_path(id))
    ]


def remove_items_with_existing_stac(grid: DataFrame, itempath: S3ItemPath) -> DataFrame:
    """Filter a dataframe to only include items which don't have an existing stac output.
    The dataframe must have an index which corresponds to ids for the given itempath.
    """
    return grid[~grid.index.isin(existing_stac_items(list(grid.index), itempath))]


def set_stac_properties(
    input_xr: DataArray | Dataset, output_xr: DataArray | Dataset
) -> Dataset | DataArray:
    """Sets an attribute called "stac_properties" in the output which is a
    dictionary containing the following properties for use in stac writing:
    "start_datetime", "end_datetime", "datetime", and "created". These are
    set from the input_xr.time coordinate. Typically, `input_xr` would be
    an array of EO data (e.g. Landsat) containing data over a range of
    dates (such as a year).
    """
    start_datetime = np.datetime_as_string(
        np.datetime64(input_xr.time.min().values, "Y"), unit="ms", timezone="UTC"
    )

    end_datetime = np.datetime_as_string(
        np.datetime64(input_xr.time.max().values, "Y")
        + np.timedelta64(1, "Y")
        - np.timedelta64(1, "s"),
        timezone="UTC",
    )
    output_xr.attrs["stac_properties"] = dict(
        start_datetime=start_datetime,
        datetime=start_datetime,
        end_datetime=end_datetime,
        created=np.datetime_as_string(np.datetime64(datetime.now()), timezone="UTC"),
    )

    return output_xr
