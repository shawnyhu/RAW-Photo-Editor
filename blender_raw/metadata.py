# SPDX-License-Identifier: GPL-3.0-or-later
"""Carry descriptive and capture metadata into an already rendered JPEG."""
import os
from datetime import date
from fractions import Fraction
from pathlib import Path
import shutil
import tempfile
import time


# Select capture fields explicitly. RAW strip offsets, preview images, MakerNote
# offsets, source ICC profiles and RAW development settings cannot describe a
# newly composited, possibly rotated, resized or color-managed JPEG.
_IMAGE_FIELDS = frozenset("""
    Make Model Artist Copyright ImageDescription XPTitle XPComment XPAuthor
    XPKeywords XPSubject
""".split())
_PHOTO_FIELDS = frozenset("""
    DateTimeOriginal DateTimeDigitized OffsetTimeOriginal OffsetTimeDigitized
    SubSecTimeOriginal SubSecTimeDigitized ExposureTime FNumber ExposureProgram
    ISOSpeedRatings SensitivityType StandardOutputSensitivity RecommendedExposureIndex
    ISOSpeed ISOSpeedLatitudeyyy ISOSpeedLatitudezzz ShutterSpeedValue ApertureValue
    BrightnessValue ExposureBiasValue MaxApertureValue SubjectDistance MeteringMode
    LightSource Flash FocalLength ExposureIndex SensingMethod ExposureMode
    WhiteBalance DigitalZoomRatio FocalLengthIn35mmFilm SceneCaptureType GainControl
    SubjectDistanceRange CameraOwnerName BodySerialNumber LensSpecification LensMake
    LensModel LensSerialNumber UserComment Temperature Humidity Pressure WaterDepth
    Acceleration CameraElevationAngle SpectralSensitivity FlashEnergy
""".split())
_IPTC_FIELDS = frozenset("""
    ObjectType ObjectAttribute ObjectName EditStatus Urgency Subject Category
    SuppCategory FixtureId Keywords LocationCode LocationName SpecialInstructions
    DateCreated TimeCreated DigitizationDate DigitizationTime Byline BylineTitle
    City SubLocation ProvinceState CountryCode CountryName TransmissionReference
    Headline Credit Source Copyright Contact Caption Writer
""".split())
_XMP_DESCRIPTIVE = {
    "dc": set("title creator subject description publisher contributor date type identifier source language relation coverage rights".split()),
    "xmp": {"CreateDate", "Rating", "Label"},
    "xmpRights": {"Marked", "Owner", "UsageTerms", "WebStatement", "Certificate"},
    "photoshop": set("DateCreated AuthorsPosition CaptionWriter City State Country Location Credit Source Headline Instructions TransmissionReference Category SupplementalCategories".split()),
    "Iptc4xmpCore": set("CiAdrCity CiAdrCtry CiAdrExtadr CiAdrPcode CiAdrRegion CiEmailWork CiTelWork CiUrlWork CreatorContactInfo Location CountryCode SubjectCode Scene IntellectualGenre".split()),
    "Iptc4xmpExt": set("LocationCreated LocationShown Event PersonInImage OrganisationInImageName OrganisationInImageCode CopyrightOwner ImageCreator CreditLine DigitalSourceType".split()),
    "aux": {"Lens", "LensInfo", "LensID", "LensSerialNumber", "SerialNumber", "OwnerName"},
    "lr": {"hierarchicalSubject"},
    "tiff": _IMAGE_FIELDS,
    "exif": _PHOTO_FIELDS | {"PhotographicSensitivity", "DateTimeOriginal", "DateTimeDigitized"},
    "exifEX": _PHOTO_FIELDS | {"PhotographicSensitivity"},
}


def _number(value):
    try:
        number = Fraction(str(value))
        return number if number > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def _lens(tags):
    model = tags.get("Exif.Photo.LensModel") or tags.get("Exif.Image.LensModel") or tags.get("Exif.Canon.LensModel")
    if model:
        return model
    spec = tags.get("Exif.Photo.LensSpecification") or tags.get("Exif.Nikon3.Lens") or ""
    numbers = [_number(part) for part in spec.split()]
    if len(numbers) == 4 and all(numbers):
        def interval(low, high):
            return f"{float(low):g}" if low == high else f"{float(low):g}–{float(high):g}"
        return f"{interval(*numbers[:2])} mm f/{interval(*numbers[2:])}"
    return ""


def _capture_time(image, exif):
    for read, keys in (
            (lambda: exif, ("Exif.Photo.DateTimeOriginal", "Exif.Image.DateTimeOriginal")),
            (lambda: image.read_xmp(encoding="latin-1"), ("Xmp.exif.DateTimeOriginal", "Xmp.photoshop.DateCreated")),
            (lambda: image.read_iptc(encoding="latin-1"), ("Iptc.Application2.DateCreated",)),
    ):
        try:
            tags = read()
        except (RuntimeError, ValueError, OverflowError):
            continue
        for key in keys:
            value = str(tags.get(key, "")).strip()
            try:
                day = date.fromisoformat(value[:10].replace(":", "-")).isoformat()
                return day + value[10:].replace("T", " ", 1)
            except ValueError:
                continue
    return ""


def capture_info(source_path):
    """Index the original once. Drawing the Reader only uses the catalog record."""
    import pyexiv2
    size = Path(source_path).stat().st_size
    info = {"size": f"{size / 1_000_000:.1f} MB" if size >= 1_000_000 else f"{size / 1000:.1f} kB"}
    try:
        source = pyexiv2.Image(os.fspath(source_path))
    except (RuntimeError, ValueError, OverflowError):
        info["captured"] = ""
        return info
    with source as image:
        try:
            tags = image.read_exif(encoding="latin-1")
        except (RuntimeError, ValueError, OverflowError):
            tags = {}
        info["captured"] = _capture_time(image, tags)
    shutter = _number(tags.get("Exif.Photo.ExposureTime") or tags.get("Exif.Image.ExposureTime"))
    aperture = _number(tags.get("Exif.Photo.FNumber") or tags.get("Exif.Image.FNumber"))
    info.update(camera=tags.get("Exif.Image.Model") or tags.get("Exif.Image.Make", ""),
                lens=_lens(tags), iso=str(tags.get("Exif.Photo.ISOSpeedRatings") or tags.get("Exif.Photo.ISOSpeed")
                                         or tags.get("Exif.Image.ISOSpeedRatings", "")),
                shutter=f"{shutter} s" if shutter else "", aperture=f"f/{float(aperture):g}" if aperture else "")
    return info


def _capture_exif(tags):
    result = {}
    for key, value in tags.items():
        _, group, field = key.split(".", 2)
        if group == "Image" and field in _PHOTO_FIELDS:
            # Some TIFF/EP cameras store exposure fields directly in IFD0.
            result.setdefault("Exif.Photo." + field, value)
        elif group == "Image" and field in _IMAGE_FIELDS:
            result[key] = value
        elif group == "Photo" and field in _PHOTO_FIELDS:
            result[key] = value
        elif group == "GPSInfo" and field.startswith("GPS"):
            result[key] = value
    return result


def _descriptive_iptc(tags):
    return {k: v for k, v in tags.items()
            if k.startswith("Iptc.Application2.") and k.rsplit(".", 1)[-1] in _IPTC_FIELDS}


def _descriptive_xmp(tags):
    result = {}
    for key, value in tags.items():
        _, namespace, field = key.split(".", 2)
        root = field.split("[", 1)[0].split("/", 1)[0]
        if root in _XMP_DESCRIPTIVE.get(namespace, ()) or (namespace == "exif" and root.startswith("GPS")):
            result[key] = value
    return result


def copy_to_jpeg(source_path, rendered_jpeg_path):
    """Update a rendered JPEG without re-encoding it. Return any reader warnings.

    source_path can point to the exact bytes materialized from a packed original.
    The caller should surface a non-empty result in its per-photo export report.
    Destination failures raise and leave the initial JPEG unchanged.
    """
    import pyexiv2

    try:
        source = pyexiv2.Image(os.fspath(source_path))
    except (RuntimeError, ValueError, OSError) as exc:
        return f"Source metadata unavailable: {exc}"

    warnings = []
    families = {}
    with source:
        for family, select in (("exif", _capture_exif), ("iptc", _descriptive_iptc),
                               ("xmp", _descriptive_xmp)):
            reader = getattr(source, "read_" + family)
            try:
                try:
                    tags = reader()
                except UnicodeDecodeError:
                    # Older cameras use single-byte EXIF/IPTC strings.
                    tags = reader(encoding="latin-1")
                fields = select(tags)
                if fields:
                    families[family] = fields
            except (RuntimeError, ValueError, OverflowError) as exc:
                warnings.append(f"{family.upper()} could not be read: {exc}")

    if families:
        target = Path(rendered_jpeg_path)
        with tempfile.TemporaryDirectory(prefix=".photo-editor-metadata-", dir=target.parent) as folder:
            working = Path(folder) / "output.jpg"
            shutil.copyfile(target, working)
            with pyexiv2.Image(str(working)) as output:
                if output.get_mime_type() != "image/jpeg":
                    raise ValueError("Expected a rendered JPEG for metadata transfer")
                if "exif" in families:
                    fields = families["exif"]
                    fields["Exif.Photo.PixelXDimension"] = str(output.get_pixel_width())
                    fields["Exif.Photo.PixelYDimension"] = str(output.get_pixel_height())
                    output.modify_exif(fields)
                if "iptc" in families:
                    # pyexiv2 serializes strings as UTF-8 by default. Declare it in IIM.
                    families["iptc"]["Iptc.Envelope.CharacterSet"] = "\x1b%G"
                    output.modify_iptc(families["iptc"])
                if "xmp" in families:
                    output.modify_xmp(families["xmp"])
            # Windows can temporarily deny replacement of a just-written JPEG
            # while another handle is open. Keep both files intact across retries.
            for delay in (.025, .05, .1, .2, .4):
                try:
                    os.replace(working, target)
                    break
                except PermissionError as exc:
                    if getattr(exc, "winerror", None) not in {5, 32, 33}:
                        raise
                    time.sleep(delay)
            else:
                os.replace(working, target)
    return "; ".join(warnings)
