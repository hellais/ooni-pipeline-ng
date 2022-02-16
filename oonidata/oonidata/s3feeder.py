#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Feeds reports from cans on public S3 bucke or local disk

Explore bucket from CLI:
AWS_PROFILE=ooni-data aws s3 ls s3://ooni-data/canned/2019-07-16/

"""

from datetime import date, timedelta, datetime
from typing import Generator, Set, NamedTuple, Any
from collections import namedtuple
from pathlib import Path
import logging
import os
import time
import gzip
import tarfile

import lz4.frame as lz4frame  # debdeps: python3-lz4
import ujson

# lz4frame appears faster than executing lz4cat: 2.4s vs 3.9s on a test file

import boto3  # debdeps: python3-boto3
from botocore import UNSIGNED as botoSigUNSIGNED
from botocore.config import Config as botoConfig

from .metrics import setup_metrics
from .mytypes import MsmtTup  # msmt bytes, msmt dict, uid
from .normalize import iter_yaml_msmt_normalized
from .utils import trivial_id

CAN_BUCKET_NAME = "ooni-data"
MC_BUCKET_NAME = "ooni-data-eu-fra"

log = logging.getLogger("fastpath")
metrics = setup_metrics(name="fastpath.s3feeder")

# suppress debug logs
for x in ("urllib3", "botocore", "s3transfer"):
    logging.getLogger(x).setLevel(logging.INFO)


def load_multiple(fn: str) -> Generator[MsmtTup, None, None]:
    """Load contents of legacy cans and minicans.
    Decompress tar archives if found.
    Yields measurements one by one as:
        (string of JSON, None, uid) or (None, msmt dict, uid)
    The uid is either taken from the filename or generated by trivial_id for
    legacy cans
    """
    # TODO: split this and handle legacy cans and post/minicans independently
    if fn.endswith(".tar.lz4"):
        # Legacy lz4 cans
        with lz4frame.open(fn) as f:
            tf = tarfile.TarFile(fileobj=f)
            while True:
                m = tf.next()
                if m is None:
                    # end of tarball
                    break
                log.debug("Loading nested %s", m.name)
                k = tf.extractfile(m)
                assert k is not None
                if m.name.endswith(".json"):
                    for line in k:
                        msm = ujson.loads(line)
                        msmt_uid = trivial_id(msm)
                        yield (None, msm, msmt_uid)

                elif m.name.endswith(".yaml"):
                    bucket_tstamp = fn.split("/")[-2]
                    rfn = f"{bucket_tstamp}/" + fn.split("/")[-1]
                    for msm in iter_yaml_msmt_normalized(k, bucket_tstamp, rfn):
                        metrics.incr("yaml_normalization")
                        msmt_uid = trivial_id(msm)
                        yield (None, msm, msmt_uid)

    elif fn.endswith(".json.lz4"):
        # Legacy lz4 json files
        with lz4frame.open(fn) as f:
            for line in f:
                msm = ujson.loads(line)
                msmt_uid = trivial_id(msm)
                yield (None, msm, msmt_uid)

    elif fn.endswith(".jsonl.gz"):
        # New JSONL files
        with gzip.open(fn) as f:
            for line in f:
                msm = ujson.loads(line)
                msmt_uid = trivial_id(msm)
                yield (None, msm, msmt_uid)

    elif fn.endswith(".yaml.lz4"):
        # Legacy lz4 yaml files
        with lz4frame.open(fn) as f:
            bucket_tstamp = fn.split("/")[-2]
            rfn = f"{bucket_tstamp}/" + fn.split("/")[-1]
            for msm in iter_yaml_msmt_normalized(f, bucket_tstamp, rfn):
                metrics.incr("yaml_normalization")
                msmt_uid = trivial_id(msm)
                yield (None, msm, msmt_uid)

    elif fn.endswith(".tar.gz"):
        # minican with missing gzipping :(
        tf = tarfile.open(fn)
        while True:
            m = tf.next()
            if m is None:
                # end of tarball
                tf.close()
                break
            log.debug("Loading %s", m.name)
            k = tf.extractfile(m)
            assert k is not None
            if not m.name.endswith(".post"):
                log.error("Unexpected filename")
                continue

            try:
                j = ujson.loads(k.read())
            except Exception:
                log.error(repr(k[:100]), exc_info=1)
                continue

            fmt = j.get("format", "")
            if fmt == "json":
                msm = j.get("content", {})
                # extract msmt_uid from filename e.g:
                # ... /20210614004521.999962_JO_signal_68eb19b439326d60.post
                msmt_uid = m.name.rsplit("/", 1)[1]
                msmt_uid = msmt_uid[:-5]
                yield (None, msm, msmt_uid)

            elif fmt == "yaml":
                log.info("Skipping YAML")

            else:
                log.info("Ignoring invalid post")

    elif fn.endswith("/index.json.gz"):
        pass

    else:
        raise RuntimeError(f"Unexpected [mini]can filename '{fn}'")


def create_s3_client():
    return boto3.client("s3", config=botoConfig(signature_version=botoSigUNSIGNED))


def list_cans_on_s3_for_a_day(s3, day: date) -> list:
    return list(
        map(lambda fe: (fe.s3path, fe.size), iter_cans_on_s3_for_a_day(s3, day))
    )


def iter_cans_on_s3_for_a_day(s3, day: date):
    """List legacy cans."""
    prefix = f"canned/{day}/"
    paginator = s3.get_paginator("list_objects_v2")
    files = []
    for r in paginator.paginate(Bucket=CAN_BUCKET_NAME, Prefix=prefix):
        if ("Contents" in r) ^ (day <= date(2020, 10, 21)):
            # The last day with cans is 2020-10-21
            log.warn("%d can files found!", len(r.get("Contents", [])))

        for f in r.get("Contents", []):
            s3path = f["Key"]
            filename = s3path.split("/")[-1]
            country_code = None
            ext = None
            if filename.endswith(".tar.lz4"):
                test_name = filename.split(".")[0].replace("_", "")
                ext = "tar.lz4"
            elif filename.endswith(".json.lz4"):
                parts = filename.split("-")
                country_code = parts[1]
                test_name = parts[3].replace("_", "")
                ext = "json.lz4"
            else:
                if filename != "index.json.gz":
                    log.warn(f"found an unexpected filename {filename}")
                continue

            file_entry = FileEntry(
                day=day,
                country_code=country_code,
                test_name=test_name,
                filename=filename,
                size=f["Size"],
                ext=ext,
                s3path=s3path,
                bucket_name=MC_BUCKET_NAME,
            )
            yield file_entry


class FileEntry(NamedTuple):
    day: date
    country_code: Any
    test_name: str
    filename: str
    size: int
    ext: str
    s3path: str
    bucket_name: str

    def output_path(self, dst_dir: Path) -> Path:
        return (
            dst_dir
            / self.test_name
            / self.country_code
            / f"{self.day:%Y-%m-%d}"
            / self.filename
        )

    def matches_filter(self, ccs: Set[str], testnames: Set[str]) -> bool:
        if self.country_code and ccs and self.country_code not in ccs:
            return False

        if self.test_name and testnames and self.test_name not in testnames:
            return False

        return True

    def log_download(self) -> None:
        s = self.size / 1024 / 1024
        d = "M"
        if s < 1:
            s = self.size / 1024
            d = "K"
        log.info(f"Downloading can {self.s3path} size {s:.1f} {d}B")


def iter_file_entries(s3, prefix: str) -> Generator[FileEntry, None, None]:
    paginator = s3.get_paginator("list_objects_v2")
    for r in paginator.paginate(Bucket=MC_BUCKET_NAME, Prefix=prefix):
        for f in r.get("Contents", []):
            s3path = f["Key"]
            filename = s3path.split("/")[-1]
            parts = filename.split("_")
            test_name, _, _, ext = parts[2].split(".", 3)
            file_entry = FileEntry(
                day=datetime.strptime(parts[0], "%Y%m%d%H").day(),
                country_code=parts[1],
                test_name=test_name,
                filename=filename,
                s3path=s3path,
                size=f["Size"],
                ext=ext,
                bucket_name=MC_BUCKET_NAME,
            )
            yield file_entry


def jsonl_in_range(
    s3, conf, start_day: date, end_day: date
) -> Generator[FileEntry, None, None]:
    legacy_prefixes = [
        f"raw/{d:%Y%m%d}"
        for d in date_interval(max(date(2020, 10, 20), start_day), end_day)
    ]
    prefixes = ["jsonl/"]
    # We have both a testname list and a country code list, we can efficiently
    # pre-filter based on prefix
    if conf.testnames and conf.ccs:
        c = itertools.product(conf.testnames, conf.ccs)
        prefixes = [f"jsonl/{tn}/{cc}/" for cc, tn in c]

    elif conf.testnames:
        prefixes = [f"jsonl/{tn}/" for tn in conf.testnames]

    # In other cases, we are going to have to list all the bucket and do
    # filtering based on filepath
    for p in prefixes + legacy_prefixes:
        for file_entry in iter_file_entries(s3, p):
            if file_entry.ext != "jsonl.gz":
                log.warn(
                    f"Found non jsonl.gz file in jsonl prefix: {file_entry.s3path}"
                )
                continue

            if not file_entry.matches_filter(conf.ccs, conf.testnames):
                continue

            if file_entry.day < start_day or file_entry.day >= end_day:
                continue

            if file_entry.size > 0:
                yield file_entry


def list_minicans_on_s3_for_a_day(
    s3, day: date, ccs: Set[str], testnames: Set[str]
) -> list:
    return list(
        map(
            lambda fe: (fe.s3path, fe.size),
            filter(
                lambda fe: fe.matches_filter(ccs, testnames),
                iter_minicans_on_s3_for_a_day(s3, day),
            ),
        )
    )


def iter_minicans_on_s3_for_a_day(s3, day: date) -> Generator[FileEntry, None, None]:
    """List minicans. Filter them by CCs and testnames
    Testnames are without underscores.
    """
    # s3cmd ls s3://ooni-data-eu-fra/raw/20210202
    tstamp = day.strftime("%Y%m%d")
    prefix = f"raw/{tstamp}/"
    files = []
    for file_entry in iter_file_entries(s3, prefix):
        if not file_entry.ext != "tar.gz":
            continue
        yield file_entry

    if (day >= date(2020, 10, 20)) ^ len(files) > 0:
        # The first day with minicans is 2020-10-20
        log.warn("%d minican files found!", len(files))


def _calculate_etr(t0, now, start_day, day, stop_day, can_num, can_tot_count) -> int:
    """Estimate total runtime in seconds.
    stop_day is not included, can_num starts from 0
    """
    tot_days_count = (stop_day - start_day).days
    elapsed = now - t0
    days_done = (day - start_day).days
    fraction_of_day_done = (can_num + 1) / float(can_tot_count)
    etr = elapsed * tot_days_count / (days_done + fraction_of_day_done)
    return etr


def _update_eta(t0, start_day, day, stop_day, can_num, can_tot_count):
    """Generate metric process_s3_measurements_eta expressed as epoch"""
    try:
        now = time.time()
        etr = _calculate_etr(t0, now, start_day, day, stop_day, can_num, can_tot_count)
        eta = t0 + etr
        metrics.gauge("process_s3_measurements_eta", eta)
    except:
        pass


def date_interval(start_day: date, end_day: date):
    today = date.today()
    if not start_day or start_day >= today:
        raise StopIteration
    day = start_day
    # the last day is not included
    stop_day = end_day if end_day < today else today
    while day < stop_day:
        yield day
        day += timedelta(days=1)


@metrics.timer("download_measurement_container")
def download_measurement_container(s3, conf, file_entry: FileEntry):
    diskf = file_entry.output_path(conf.s3cachedir)
    if diskf.exists() and file_entry.size == diskf.stat().st_size:
        metrics.incr("cache_hit")
        diskf.touch(exist_ok=True)
        return diskf
    metrics.incr("cache_miss")

    file_entry.log_download()

    def _cb(bytes_count):
        if _cb.start_time is None:
            _cb.start_time = time.time()
            _cb.count = bytes_count
            return
        _cb.count += bytes_count
        _cb.total_count += bytes_count
        metrics.gauge("s3_download_percentage", _cb.total_count / _cb.total_size * 100)
        try:
            speed = _cb.count / 131_072 / (time.time() - _cb.start_time)
            metrics.gauge("s3_download_speed_avg_Mbps", speed)
        except ZeroDivisionError:
            pass

    _cb.total_size = file_entry.size
    _cb.total_count = 0
    _cb.start_time = None

    diskf.parent.mkdir(parents=True, exist_ok=True)
    tmpf = diskf.with_suffix(".s3tmp")
    with tmpf.open("wb") as f:
        s3.download_fileobj(file_entry.bucket_name, file_entry.s3path, f, Callback=_cb)
        f.flush()
        os.fsync(f.fileno())
    metrics.gauge("fetching", 0)
    tmpf.rename(diskf)
    assert file_entry.size == diskf.stat().st_size
    metrics.gauge("s3_download_speed_avg_Mbps", 0)
    return diskf


def stream_measurements(
    s3, conf, file_entries: Generator[FileEntry, None, None]
) -> Generator[MsmtTup, None, None]:
    for fe in file_entries:
        if not fe.matches_filter(conf.ccs, conf.testnames):
            continue
        mc = download_measurement_container(s3, conf, fe)
        try:
            yield from load_multiple(mc.as_posix())
        except Exception as e:
            log.error(str(e), exc_info=True)
        if not conf.keep_s3_cache:
            try:
                mc.unlink()
            except FileNotFoundError:
                pass


def stream_cans(conf, start_day: date, end_day: date) -> Generator[MsmtTup, None, None]:
    """Stream cans from S3"""
    log.info("Fetching older cans from S3")
    t0 = time.time()
    s3 = create_s3_client()
    for day in date_interval(start_day, end_day):
        log.info("Processing day %s", day)

        can_file_entries = itertools.chain(
            iter_cans_on_s3_for_a_day(s3, day), iter_minicans_on_s3_for_a_day(s3, day)
        )
        yield from stream_measurements(s3, conf, can_file_entries)

    if end_day:
        log.info(f"Reached {end_day}, streaming cans from S3 finished")
        return


def stream_jsonl(
    conf, start_day: date, end_day: date
) -> Generator[MsmtTup, None, None]:
    """Stream jsonl from S3"""
    log.info("Fetching older cans from S3")
    s3 = create_s3_client()
    yield from stream_measurements(
        s3, conf, jsonl_in_range(s3, conf, start_day, end_day)
    )

    if end_day:
        log.info(f"Reached {end_day}, streaming cans from S3 finished")
        return
