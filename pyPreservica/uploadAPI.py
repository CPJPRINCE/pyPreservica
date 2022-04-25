import base64
import csv
import shutil
import tempfile
import uuid
import xml
from datetime import datetime
from time import sleep
from xml.dom import minidom
from xml.etree import ElementTree
from xml.etree.ElementTree import Element, SubElement

import boto3
import cryptography
import s3transfer.tasks
import s3transfer.upload
from boto3.s3.transfer import TransferConfig, S3Transfer
from botocore.config import Config
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from requests.auth import HTTPBasicAuth
from s3transfer import S3UploadFailedError
from tqdm import tqdm

from pyPreservica.common import *
from pyPreservica.common import _make_stored_zipfile

logger = logging.getLogger(__name__)

MB = 1024 * 1024
GB = 1024 ** 3
transfer_config = TransferConfig(multipart_threshold=int((1 * GB) / 16))


def upload_file(self, filename, bucket, key,
                callback=None, extra_args=None):
    """Upload a file to an S3 object.

    Variants have also been injected into S3 client, Bucket and Object.
    You don't have to use S3Transfer.upload_file() directly.

    .. seealso::
        :py:meth:`S3.Client.upload_file`
        :py:meth:`S3.Client.upload_fileobj`
    """
    if not isinstance(filename, str):
        raise ValueError('Filename must be a string')

    subscribers = self._get_subscribers(callback)
    future = self._manager.upload(
        filename, bucket, key, extra_args, subscribers)
    try:
        return future.result()
    # If a client error was raised, add the backwards compatibility layer
    # that raises a S3UploadFailedError. These specific errors were only
    # ever thrown for upload_parts but now can be thrown for any related
    # client error.
    except ClientError as e:
        raise S3UploadFailedError(
            "Failed to upload %s to %s: %s" % (
                filename, '/'.join([bucket, key]), e))


class PutObjectTask(s3transfer.tasks.Task):
    # Copied from s3transfer/upload.py, changed to return the result of client.put_object.
    def _main(self, client, fileobj, bucket, key, extra_args):
        with fileobj as body:
            response = client.put_object(Bucket=bucket, Key=key, Body=body, **extra_args)
            return response


class CompleteMultipartUploadTask(s3transfer.tasks.Task):
    # Copied from s3transfer/tasks.py, changed to return a result.
    def _main(self, client, bucket, key, upload_id, parts, extra_args):
        return client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
            **extra_args,
        )


s3transfer.upload.PutObjectTask = PutObjectTask
s3transfer.upload.CompleteMultipartUploadTask = CompleteMultipartUploadTask


def prettify(elem):
    """Return a pretty-printed XML string for the Element.
    """
    rough_string = xml.etree.ElementTree.tostring(elem, 'utf-8')
    re_parsed = minidom.parseString(rough_string)
    return re_parsed.toprettyxml(indent="  ")


def __create_io__(xip=None, file_name=None, parent_folder=None, **kwargs):
    if xip is None:
        xip = Element('XIP')
    assert xip is not None
    xip.set('xmlns', 'http://preservica.com/XIP/v6.0')
    io = SubElement(xip, 'InformationObject')
    ref = SubElement(io, 'Ref')

    if 'IO_Identifier_callback' in kwargs:
        ident_callback = kwargs.get('IO_Identifier_callback')
        ref.text = ident_callback()
    else:
        ref.text = str(uuid.uuid4())

    title = SubElement(io, 'Title')
    title.text = kwargs.get('Title', file_name)
    description = SubElement(io, 'Description')
    description.text = kwargs.get('Description', file_name)
    security = SubElement(io, 'SecurityTag')
    security.text = kwargs.get('SecurityTag', "open")
    custom_type = SubElement(io, 'CustomType')
    custom_type.text = kwargs.get('CustomType', "")
    parent = SubElement(io, 'Parent')

    if hasattr(parent_folder, "reference"):
        parent.text = parent_folder.reference
    elif isinstance(parent_folder, str):
        parent.text = parent_folder

    return xip, ref.text


def __make_representation__(xip, rep_name, rep_type, io_ref):
    representation = SubElement(xip, 'Representation')
    io_link = SubElement(representation, 'InformationObject')
    io_link.text = io_ref
    access_name = SubElement(representation, 'Name')
    access_name.text = rep_name
    access_type = SubElement(representation, 'Type')
    access_type.text = rep_type
    content_objects = SubElement(representation, 'ContentObjects')
    content_object = SubElement(content_objects, 'ContentObject')
    content_object_ref = str(uuid.uuid4())
    content_object.text = content_object_ref
    return content_object_ref


def __make_content_objects__(xip, content_title, co_ref, io_ref, tag, content_description, content_type):
    content_object = SubElement(xip, 'ContentObject')
    ref_element = SubElement(content_object, "Ref")
    ref_element.text = co_ref
    title = SubElement(content_object, "Title")
    title.text = content_title
    description = SubElement(content_object, "Description")
    description.text = content_description
    security_tag = SubElement(content_object, "SecurityTag")
    security_tag.text = tag
    custom_type = SubElement(content_object, "CustomType")
    custom_type.text = content_type
    parent = SubElement(content_object, "Parent")
    parent.text = io_ref


def __make_generation__(xip, filename, co_ref, generation_label):
    generation = SubElement(xip, 'Generation', {"original": "true", "active": "true"})
    content_object = SubElement(generation, "ContentObject")
    content_object.text = co_ref
    label = SubElement(generation, "Label")
    if generation_label:
        label.text = generation_label
    else:
        label.text = os.path.splitext(filename)[0]
    effective_date = SubElement(generation, "EffectiveDate")
    effective_date.text = datetime.now().isoformat()
    bitstreams = SubElement(generation, "Bitstreams")
    bitstream = SubElement(bitstreams, "Bitstream")
    bitstream.text = filename
    SubElement(generation, "Formats")
    SubElement(generation, "Properties")


def __make_bitstream__(xip, file_name, full_path, callback):
    bitstream = SubElement(xip, 'Bitstream')
    filename_element = SubElement(bitstream, "Filename")
    filename_element.text = file_name
    filesize = SubElement(bitstream, "FileSize")
    file_stats = os.stat(full_path)
    filesize.text = str(file_stats.st_size)
    physical_location = SubElement(bitstream, "PhysicalLocation")
    fixities = SubElement(bitstream, "Fixities")
    fixity_result = callback(file_name, full_path)
    if type(fixity_result) == tuple:
        fixity = SubElement(fixities, "Fixity")
        fixity_algorithm_ref = SubElement(fixity, "FixityAlgorithmRef")
        fixity_value = SubElement(fixity, "FixityValue")
        fixity_algorithm_ref.text = fixity_result[0]
        fixity_value.text = fixity_result[1]
    elif type(fixity_result) == dict:
        for key, val in fixity_result.items():
            fixity = SubElement(fixities, "Fixity")
            fixity_algorithm_ref = SubElement(fixity, "FixityAlgorithmRef")
            fixity_value = SubElement(fixity, "FixityValue")
            fixity_algorithm_ref.text = key
            fixity_value.text = val
    else:
        logger.error("Could Not Find Fixity Value")
        raise RuntimeError("Could Not Find Fixity Value")


def __make_representation_multiple_co__(xip, rep_name, rep_type, rep_files, io_ref):
    representation = SubElement(xip, 'Representation')
    io_link = SubElement(representation, 'InformationObject')
    io_link.text = io_ref
    access_name = SubElement(representation, 'Name')
    access_name.text = rep_name
    access_type = SubElement(representation, 'Type')
    access_type.text = rep_type
    content_objects = SubElement(representation, 'ContentObjects')
    refs_dict = {}
    for f in rep_files:
        content_object = SubElement(content_objects, 'ContentObject')
        content_object_ref = str(uuid.uuid4())
        content_object.text = content_object_ref
        refs_dict[content_object_ref] = f
    return refs_dict


def cvs_to_cmis_xslt(csv_file, xml_namespace, root_element, title="Metadata Title", export_folder=None,
                     additional_namespaces=None):
    """
            Create a custom CMIS transform to display metadata within UA.

    """
    headers = set()
    with open(csv_file, encoding='utf-8-sig', newline='') as csvfile:
        reader = csv.reader(csvfile)
        for row in reader:
            for header in row:
                xml_tag = header.strip()
                xml_tag = xml_tag.replace(" ", "")
                xml_tag = xml_tag.replace("-", "")
                headers.add(xml_tag)
            break

    namespaces = {"version": "2.0",
                  "xmlns:xsl": "http://www.w3.org/1999/XSL/Transform",
                  "xmlns:fn": "http://www.w3.org/2005/xpath-functions",
                  "xmlns:xs": "http://www.w3.org/2001/XMLSchema",
                  "xmlns:csv": xml_namespace,
                  "xmlns": "http://www.tessella.com/sdb/cmis/metadata",
                  "exclude-result-prefixes": "csv"}

    if additional_namespaces is not None:
        for prefix, uri in additional_namespaces.items():
            namespaces["xmlns:" + prefix] = uri

    xml_stylesheet = xml.etree.ElementTree.Element("xsl:stylesheet", namespaces)

    xml.etree.ElementTree.SubElement(xml_stylesheet, "xsl:output", {"method": "xml", "indent": "yes"})

    xml_template = xml.etree.ElementTree.SubElement(xml_stylesheet, "xsl:template", {"match": "csv:" + root_element})

    xml_group = xml.etree.ElementTree.SubElement(xml_template, "group")

    xml_title = xml.etree.ElementTree.SubElement(xml_group, "title")
    xml_title.text = title

    xml_templates = xml.etree.ElementTree.SubElement(xml_group, "xsl:apply-templates")

    elements = ""

    for header in headers:
        if ":" in header:
            elements = elements + "|" + header
        else:
            elements = elements + "|csv:" + header

    elements = elements[1:]

    xml_matches = xml.etree.ElementTree.SubElement(xml_stylesheet, "xsl:template", {"match": elements})

    xml_item = xml.etree.ElementTree.SubElement(xml_matches, "item")
    xml_name = xml.etree.ElementTree.SubElement(xml_item, "name")
    xml_name_value = xml.etree.ElementTree.SubElement(xml_name, "xsl:value-of", {
        "select": "fn:replace(translate(local-name(), '_', ' '), '([a-z])([A-Z])', '$1 $2')"})

    xml_value = xml.etree.ElementTree.SubElement(xml_item, "value")
    xml_value_value = xml.etree.ElementTree.SubElement(xml_value, "xsl:value-of", {"select": "."})

    xml_type = xml.etree.ElementTree.SubElement(xml_item, "type")
    xml_type_value = xml.etree.ElementTree.SubElement(xml_type, "xsl:value-of", {
        "select": "fn:replace(translate(local-name(), '_', ' '), '([a-z])([A-Z])', '$1 $2')"})

    xml_request = xml.etree.ElementTree.tostring(xml_stylesheet, encoding='utf-8', xml_declaration=True)
    cmis_xslt = root_element + "-cmis.xslt"
    if export_folder is not None:
        cmis_xslt = os.path.join(export_folder, cmis_xslt)
    file = open(cmis_xslt, "wt", encoding="utf-8")
    file.write(xml_request.decode("utf-8"))
    file.close()
    return cmis_xslt


def cvs_to_xsd(csv_file, xml_namespace, root_element, export_folder=None, additional_namespaces=None):
    """
        Create a XSD definition based on the csv file

    """
    headers = set()
    with open(csv_file, encoding='utf-8-sig', newline='') as csvfile:
        reader = csv.reader(csvfile)
        for row in reader:
            for header in row:
                xml_tag = header.strip()
                xml_tag = xml_tag.replace(" ", "")
                xml_tag = xml_tag.replace("-", "")
                headers.add(xml_tag)
            break

    namespaces = {"xmlns:xs": "http://www.w3.org/2001/XMLSchema",
                  "attributeFormDefault": "unqualified",
                  "elementFormDefault": "qualified",
                  "targetNamespace": xml_namespace}

    if additional_namespaces is not None:
        for prefix, uri in additional_namespaces.items():
            namespaces["xmlns:" + prefix.trim()] = uri.trim()

    xml_schema = xml.etree.ElementTree.Element("xs:schema", namespaces)

    if additional_namespaces is not None:
        for prefix, namespace in additional_namespaces.items():
            xml_import = xml.etree.ElementTree.SubElement(xml_schema, "xs:import", {"namespace": namespace})

    xml_element = xml.etree.ElementTree.SubElement(xml_schema, "xs:element", {"name": root_element})

    xml_complex_type = xml.etree.ElementTree.SubElement(xml_element, "xs:complexType")
    xml_sequence = xml.etree.ElementTree.SubElement(xml_complex_type, "xs:sequence")
    for header in headers:
        if ":" in header:
            prefix, sep, tag = header.partition(":")
            try:
                namespace = additional_namespaces[prefix]
                xml.etree.ElementTree.SubElement(xml_sequence, "xs:element",
                                                 {"ref": header, "xmlns:" + prefix: namespace})
            except KeyError:
                xml.etree.ElementTree.SubElement(xml_sequence, "xs:element", {"type": "xs:string", "name": header})
        else:
            xml.etree.ElementTree.SubElement(xml_sequence, "xs:element", {"type": "xs:string", "name": header})

    xml_request = xml.etree.ElementTree.tostring(xml_schema, encoding='utf-8', xml_declaration=True)

    xsd_file = root_element + ".xsd"
    if export_folder is not None:
        xsd_file = os.path.join(export_folder, xsd_file)
    file = open(xsd_file, "wt", encoding="utf-8")
    file.write(xml_request.decode("utf-8"))
    file.close()
    return xsd_file


def csv_to_search_xml(csv_file, xml_namespace, root_element, title="Metadata Title", export_folder=None,
                      additional_namespaces=None):
    """
        Create a custom Preservica search index based on the columns in a csv file

    """
    headers = set()
    with open(csv_file, encoding='utf-8-sig', newline='') as csvfile:
        reader = csv.reader(csvfile)
        for row in reader:
            for header in row:
                xml_tag = header.strip()
                xml_tag = xml_tag.replace(" ", "")
                xml_tag = xml_tag.replace("-", "")
                headers.add(xml_tag)
            break

    xml_index = xml.etree.ElementTree.Element("index", {"xmlns": "http://www.preservica.com/customindex/v1"})

    short_name = "csv"

    xml_schema_name = xml.etree.ElementTree.SubElement(xml_index, "schemaName")
    xml_schema_name.text = title
    xml_schema_uri = xml.etree.ElementTree.SubElement(xml_index, "schemaUri")
    xml_schema_uri.text = xml_namespace
    xml_short_name = xml.etree.ElementTree.SubElement(xml_index, "shortName")
    xml_short_name.text = short_name

    for header in headers:
        if ":" in header:
            xpath_expression = f"//{short_name}:{root_element}/{header}"
        else:
            xpath_expression = f"//{short_name}:{root_element}/{short_name}:{header}"

        attr = {"indexName": header, "displayName": header,
                "xpath": xpath_expression,
                "indexType": "STRING_DEFAULT"}
        xml_term = xml.etree.ElementTree.SubElement(xml_index, "term", attr)

    if additional_namespaces is not None:
        for prefix, uri in additional_namespaces.items():
            xml.etree.ElementTree.SubElement(xml_index, "namespaceMapping", {"key": prefix, "value": uri})

    xml_request = xml.etree.ElementTree.tostring(xml_index, encoding='utf-8', xml_declaration=True)
    search_xml = root_element + "-index.xml"
    if export_folder is not None:
        search_xml = os.path.join(export_folder, search_xml)
    file = open(search_xml, "wt", encoding="utf-8")
    file.write(xml_request.decode("utf-8"))
    file.close()
    return search_xml


def cvs_to_xml(csv_file, xml_namespace, root_element, file_name_column="filename", export_folder=None,
               additional_namespaces=None):
    """
        Export the rows of a CSV file as XML metadata documents which can be added to Preservica assets

        :param str csv_file: Path to the csv file
        :param str xml_namespace: The XML namespace for the created XML documents
        :param str root_element: The root element for the XML documents
        :param str file_name_column: The CSV column which should be used to name the xml files
        :param str export_folder: The path to the export folder
        :param dict additional_namespaces: A map of prefix, uris to use as additional namespaces

    """
    headers = list()
    link_column_id = 0
    with open(csv_file, encoding='utf-8-sig', newline='') as csvfile:
        reader = csv.reader(csvfile)
        for row in reader:
            col_id = 0
            for header in row:
                col_id += 1
                if header == file_name_column:
                    link_column_id = col_id
                xml_tag = header.strip()
                xml_tag = xml_tag.replace(" ", "")
                xml_tag = xml_tag.replace("-", "")
                headers.append(xml_tag)
            break
        if link_column_id > 0:
            namespaces = {"xmlns": xml_namespace}
            if additional_namespaces is not None:
                for prefix, uri in additional_namespaces.items():
                    namespaces["xmlns:" + prefix] = uri
            for row in reader:
                col_id = 0
                xml_object = xml.etree.ElementTree.Element(root_element, namespaces)
                for value, header in zip(row, headers):
                    col_id += 1
                    xml.etree.ElementTree.SubElement(xml_object, header).text = value
                    if col_id == link_column_id:
                        file_name = value
                xml_request = xml.etree.ElementTree.tostring(xml_object, encoding='utf-8', xml_declaration=True)
                name = file_name + ".xml"
                name = sanitize(name)
                if export_folder is not None:
                    name = os.path.join(export_folder, name)
                file = open(name, "wt", encoding="utf-8")
                file.write(xml_request.decode("utf-8"))
                file.close()
                yield name


def generic_asset_package(preservation_files_dict=None, access_files_dict=None, export_folder=None,
                          parent_folder=None, compress=True, **kwargs):
    # some basic validation
    if export_folder is None:
        export_folder = tempfile.gettempdir()
    if not os.path.isdir(export_folder):
        logger.error("Export Folder Does Not Exist")
        raise RuntimeError(export_folder, "Export Folder Does Not Exist")
    if parent_folder is None:
        logger.error("You must specify a parent folder for the package asset")
        raise RuntimeError("You must specify a parent folder for the package asset")

    io_ref = None
    xip = None
    default_asset_title = None
    preservation_representation_refs_dict = {}
    access_representation_refs_dict = {}

    security_tag = kwargs.get('SecurityTag', "open")
    content_type = kwargs.get('CustomType', "")

    if not compress:
        shutil.register_archive_format("szip", _make_stored_zipfile, None, "UnCompressed ZIP file")

    has_preservation_files = bool((preservation_files_dict is not None) and (len(preservation_files_dict) > 0))
    has_access_files = bool((access_files_dict is not None) and (len(access_files_dict) > 0))

    if has_preservation_files:
        if default_asset_title is None:
            key = list(preservation_files_dict.keys())[0]
            preservation_files_list = preservation_files_dict[key]
            default_asset_title = os.path.splitext(os.path.basename(preservation_files_list[0]))[0]

        # create the asset
        xip, io_ref = __create_io__(file_name=default_asset_title, parent_folder=parent_folder, **kwargs)

    if has_access_files:
        if default_asset_title is None:
            key = list(access_files_dict.keys())[0]
            access_files_list = access_files_dict[key]
            default_asset_title = os.path.splitext(os.path.basename(access_files_list[0]))[0]

        if io_ref is None:
            xip, io_ref = __create_io__(file_name=default_asset_title, parent_folder=parent_folder, **kwargs)

    # loop over preservation_files_map

    if has_preservation_files:
        for representation_name in preservation_files_dict.keys():
            preservation_files_list = preservation_files_dict[representation_name]
            preservation_refs_dict = __make_representation_multiple_co__(xip, rep_name=representation_name,
                                                                         rep_type="Preservation",
                                                                         rep_files=preservation_files_list,
                                                                         io_ref=io_ref)
            preservation_representation_refs_dict[representation_name] = preservation_refs_dict

    if has_access_files:
        for representation_name in access_files_dict.keys():
            access_files_list = access_files_dict[representation_name]
            access_refs_dict = __make_representation_multiple_co__(xip, rep_name=representation_name, rep_type="Access",
                                                                   rep_files=access_files_list, io_ref=io_ref)
            access_representation_refs_dict[representation_name] = access_refs_dict

    if has_preservation_files:
        for representation_name in preservation_representation_refs_dict.keys():
            preservation_refs_dict = preservation_representation_refs_dict[representation_name]
            for content_ref, filename in preservation_refs_dict.items():
                default_content_objects_title = os.path.splitext(os.path.basename(filename))[0]

                preservation_content_title = kwargs.get('Preservation_Content_Title', default_content_objects_title)
                preservation_content_description = kwargs.get('Preservation_Content_Description',
                                                              default_content_objects_title)

                if isinstance(preservation_content_title, dict):
                    preservation_content_title = preservation_content_title.get("filename",
                                                                                default_content_objects_title)

                if isinstance(preservation_content_description, dict):
                    preservation_content_description = preservation_content_description.get("filename",
                                                                                            default_content_objects_title)

                __make_content_objects__(xip, preservation_content_title, content_ref, io_ref, security_tag,
                                         preservation_content_description, content_type)

    if has_access_files:
        for representation_name in access_representation_refs_dict.keys():
            access_refs_dict = access_representation_refs_dict[representation_name]
            for content_ref, filename in access_refs_dict.items():
                default_content_objects_title = os.path.splitext(os.path.basename(filename))[0]

                access_content_title = kwargs.get('Access_Content_Title', default_content_objects_title)
                access_content_description = kwargs.get('Access_Content_Description', default_content_objects_title)

                if isinstance(access_content_title, dict):
                    access_content_title = access_content_title.get("filename", default_content_objects_title)

                if isinstance(access_content_description, dict):
                    access_content_description = access_content_title.get("filename", default_content_objects_title)

                __make_content_objects__(xip, access_content_title, content_ref, io_ref, security_tag,
                                         access_content_description, content_type)

    if has_preservation_files:
        for representation_name in preservation_representation_refs_dict.keys():
            preservation_refs_dict = preservation_representation_refs_dict[representation_name]
            preservation_generation_label = kwargs.get('Preservation_Generation_Label', "")
            for content_ref, filename in preservation_refs_dict.items():
                preservation_file_name = os.path.basename(filename)
                __make_generation__(xip, preservation_file_name, content_ref, preservation_generation_label)

    if has_access_files:
        for representation_name in access_representation_refs_dict.keys():
            access_refs_dict = access_representation_refs_dict[representation_name]
            access_generation_label = kwargs.get('Access_Generation_Label', "")
            for content_ref, filename in access_refs_dict.items():
                access_file_name = os.path.basename(filename)
                __make_generation__(xip, access_file_name, content_ref, access_generation_label)

    if has_preservation_files:

        if 'Preservation_files_fixity_callback' in kwargs:
            callback = kwargs.get('Preservation_files_fixity_callback')
        else:
            callback = Sha1FixityCallBack()
        for representation_name in preservation_representation_refs_dict.keys():
            preservation_refs_dict = preservation_representation_refs_dict[representation_name]
            for content_ref, filename in preservation_refs_dict.items():
                preservation_file_name = os.path.basename(filename)
                __make_bitstream__(xip, preservation_file_name, filename, callback)

    if has_access_files:

        if 'Access_files_fixity_callback' in kwargs:
            callback = kwargs.get('Access_files_fixity_callback')
        else:
            callback = Sha1FixityCallBack()

        for representation_name in access_representation_refs_dict.keys():
            access_refs_dict = access_representation_refs_dict[representation_name]
            for content_ref, filename in access_refs_dict.items():
                access_file_name = os.path.basename(filename)
                __make_bitstream__(xip, access_file_name, filename, callback)

    if 'Identifiers' in kwargs:
        identifier_map = kwargs.get('Identifiers')
        for identifier_key, identifier_value in identifier_map.items():
            if identifier_key:
                if identifier_value:
                    identifier = SubElement(xip, 'Identifier')
                    id_type = SubElement(identifier, "Type")
                    id_type.text = identifier_key
                    id_value = SubElement(identifier, "Value")
                    id_value.text = identifier_value
                    id_io = SubElement(identifier, "Entity")
                    id_io.text = io_ref

    if 'Asset_Metadata' in kwargs:
        metadata_map = kwargs.get('Asset_Metadata')
        for metadata_ns, metadata_path in metadata_map.items():
            if metadata_ns:
                if metadata_path:
                    if os.path.exists(metadata_path) and os.path.isfile(metadata_path):
                        descriptive_metadata = xml.etree.ElementTree.parse(source=metadata_path)
                        metadata = SubElement(xip, 'Metadata', {'schemaUri': metadata_ns})
                        metadata_ref = SubElement(metadata, 'Ref')
                        metadata_ref.text = str(uuid.uuid4())
                        entity = SubElement(metadata, 'Entity')
                        entity.text = io_ref
                        content = SubElement(metadata, 'Content')
                        content.append(descriptive_metadata.getroot())

    if xip is not None:
        export_folder = export_folder
        top_level_folder = os.path.join(export_folder, io_ref)
        os.mkdir(top_level_folder)
        inner_folder = os.path.join(top_level_folder, io_ref)
        os.mkdir(inner_folder)
        os.mkdir(os.path.join(inner_folder, "content"))
        metadata_path = os.path.join(inner_folder, "metadata.xml")
        metadata = open(metadata_path, "wt", encoding='utf-8')
        metadata.write(prettify(xip))
        metadata.close()
        for representation_name in preservation_representation_refs_dict.keys():
            preservation_refs_dict = preservation_representation_refs_dict[representation_name]
            for content_ref, filename in preservation_refs_dict.items():
                src_file = filename
                dst_file = os.path.join(os.path.join(inner_folder, "content"), os.path.basename(filename))
                shutil.copyfile(src_file, dst_file)
        for representation_name in access_representation_refs_dict.keys():
            access_refs_dict = access_representation_refs_dict[representation_name]
            for content_ref, filename in access_refs_dict.items():
                src_file = filename
                dst_file = os.path.join(os.path.join(inner_folder, "content"), os.path.basename(filename))
                shutil.copyfile(src_file, dst_file)
        if compress:
            shutil.make_archive(top_level_folder, 'zip', top_level_folder)
        else:
            shutil.make_archive(top_level_folder, 'szip', top_level_folder)
        shutil.rmtree(top_level_folder)
        return top_level_folder + ".zip"


def multi_asset_package(asset_file_list=None, export_folder=None, parent_folder=None, compress=True, **kwargs):
    """
    Create a package containing multiple assets, all the assets are ingested into the same parent folder provided
    by the parent_folder argument.

    :param asset_file_list: List of files. One asset per file
    :param export_folder:   Location where the package is written to
    :param parent_folder:   The folder the assets will be ingested into
    :param compress:        Bool, compress the package
    :param kwargs:
    :return:
    """

    # some basic validation
    if export_folder is None:
        export_folder = tempfile.gettempdir()
    if not os.path.isdir(export_folder):
        logger.error("Export Folder Does Not Exist")
        raise RuntimeError(export_folder, "Export Folder Does Not Exist")
    if parent_folder is None:
        logger.error("You must specify a parent folder for the package asset")
        raise RuntimeError("You must specify a parent folder for the package asset")

    security_tag = kwargs.get('SecurityTag', "open")
    content_type = kwargs.get('CustomType', "")

    if not compress:
        shutil.register_archive_format("szip", _make_stored_zipfile, None, "UnCompressed ZIP file")

    if 'Preservation_files_fixity_callback' in kwargs:
        fixity_callback = kwargs.get('Preservation_files_fixity_callback')
    else:
        fixity_callback = Sha1FixityCallBack()

    package_id = str(uuid.uuid4())
    export_folder = export_folder
    top_level_folder = os.path.join(export_folder, package_id)
    os.mkdir(top_level_folder)
    inner_folder = os.path.join(top_level_folder, package_id)
    os.mkdir(inner_folder)
    os.mkdir(os.path.join(inner_folder, "content"))

    asset_map = dict()
    xip = Element('XIP')
    for file in asset_file_list:
        default_asset_title = os.path.splitext(os.path.basename(file))[0]
        xip, io_ref = __create_io__(xip, file_name=default_asset_title, parent_folder=parent_folder, **kwargs)
        asset_map[file] = io_ref
        representation = SubElement(xip, 'Representation')
        io_link = SubElement(representation, 'InformationObject')
        io_link.text = io_ref
        access_name = SubElement(representation, 'Name')
        access_name.text = "Preservation"
        access_type = SubElement(representation, 'Type')
        access_type.text = "Preservation"
        content_objects = SubElement(representation, 'ContentObjects')
        content_object = SubElement(content_objects, 'ContentObject')
        content_object_ref = str(uuid.uuid4())
        content_object.text = content_object_ref

        default_content_objects_title = os.path.splitext(os.path.basename(file))[0]
        content_object = SubElement(xip, 'ContentObject')
        ref_element = SubElement(content_object, "Ref")
        ref_element.text = content_object_ref
        title = SubElement(content_object, "Title")
        title.text = default_content_objects_title
        description = SubElement(content_object, "Description")
        description.text = default_content_objects_title
        security_tag_element = SubElement(content_object, "SecurityTag")
        security_tag_element.text = security_tag
        custom_type = SubElement(content_object, "CustomType")
        custom_type.text = content_type
        parent = SubElement(content_object, "Parent")
        parent.text = io_ref

        generation = SubElement(xip, 'Generation', {"original": "true", "active": "true"})
        content_object = SubElement(generation, "ContentObject")
        content_object.text = content_object_ref
        label = SubElement(generation, "Label")
        label.text = os.path.splitext(os.path.basename(file))[0]
        effective_date = SubElement(generation, "EffectiveDate")
        effective_date.text = datetime.now().isoformat()
        bitstreams = SubElement(generation, "Bitstreams")
        bitstream = SubElement(bitstreams, "Bitstream")
        bitstream.text = os.path.basename(file)
        SubElement(generation, "Formats")
        SubElement(generation, "Properties")

        bitstream = SubElement(xip, 'Bitstream')
        filename_element = SubElement(bitstream, "Filename")
        filename_element.text = os.path.basename(file)
        filesize = SubElement(bitstream, "FileSize")
        file_stats = os.stat(file)
        filesize.text = str(file_stats.st_size)
        physical_location = SubElement(bitstream, "PhysicalLocation")
        fixities = SubElement(bitstream, "Fixities")
        fixity_result = fixity_callback(filename_element.text, file)
        if type(fixity_result) == tuple:
            fixity = SubElement(fixities, "Fixity")
            fixity_algorithm_ref = SubElement(fixity, "FixityAlgorithmRef")
            fixity_value = SubElement(fixity, "FixityValue")
            fixity_algorithm_ref.text = fixity_result[0]
            fixity_value.text = fixity_result[1]
        elif type(fixity_result) == dict:
            for key, val in fixity_result.items():
                fixity = SubElement(fixities, "Fixity")
                fixity_algorithm_ref = SubElement(fixity, "FixityAlgorithmRef")
                fixity_value = SubElement(fixity, "FixityValue")
                fixity_algorithm_ref.text = key
                fixity_value.text = val
        else:
            logger.error("Could Not Find Fixity Value")
            raise RuntimeError("Could Not Find Fixity Value")

        if 'Identifiers' in kwargs:
            identifier_map = kwargs.get('Identifiers')
            if str(file) in identifier_map:
                identifier_map_values = identifier_map[str(file)]
                for identifier_key, identifier_value in identifier_map_values.items():
                    if identifier_key:
                        if identifier_value:
                            identifier = SubElement(xip, 'Identifier')
                            id_type = SubElement(identifier, "Type")
                            id_type.text = identifier_key
                            id_value = SubElement(identifier, "Value")
                            id_value.text = identifier_value
                            id_io = SubElement(identifier, "Entity")
                            id_io.text = io_ref

        src_file = file
        dst_file = os.path.join(os.path.join(inner_folder, "content"), os.path.basename(file))
        shutil.copyfile(src_file, dst_file)

    if xip is not None:
        metadata_path = os.path.join(inner_folder, "metadata.xml")
        metadata = open(metadata_path, "wt", encoding='utf-8')
        metadata.write(prettify(xip))
        metadata.close()
        if compress:
            shutil.make_archive(top_level_folder, 'zip', top_level_folder)
        else:
            shutil.make_archive(top_level_folder, 'szip', top_level_folder)
        shutil.rmtree(top_level_folder)
        return top_level_folder + ".zip"


def complex_asset_package(preservation_files_list=None, access_files_list=None, export_folder=None,
                          parent_folder=None, compress=True, **kwargs):
    """

            Create a Preservica package containing a single Asset from a multiple preservation files
            and optional access files.
            The Asset contains multiple Content Objects within each representation.

            If only the preservation files are provided the asset has one representation


            :param list preservation_files_list: Paths to the preservation files
            :param list access_files_list: Paths to the access files
            :param str export_folder: The package location folder
            :param Folder parent_folder: The folder to ingest the asset into
            :param bool compress: Compress the ZIP file
            :param str Title: Asset Title
            :param str Description: Asset Description
            :param str SecurityTag: Asset SecurityTag
            :param str CustomType: Asset CustomType
            :param str Preservation_Content_Title: Title of the Preservation Representation Content Object
            :param str Preservation_Content_Description: Description of the Preservation Representation Content Object
            :param str Access_Content_Title: Title of the Access Representation Content Object
            :param str Access_Content_Description: Description of the Access Representation Content Object
            :param dict Asset_Metadata: Dictionary of Asset metadata documents
            :param dict Identifiers: Dictionary of Asset rd party identifiers




        optional kwargs map
        'Title'                                 Asset Title
        'Description'                           Asset Description
        'SecurityTag'                           Asset Security Tag
        'CustomType'                            Asset Type
        'Preservation_Content_Title'            Content Object Title of the Preservation Object
        'Preservation_Content_Description'      Content Object Description of the Preservation Object
        'Access_Content_Title'                  Content Object Title of the Access Object
        'Access_Content_Description'            Content Object Description of the Access Object
        'Preservation_Generation_Label'         Generation Label for the Preservation Object
        'Access_Generation_Label'               Generation Label for the Access Object
        'Asset_Metadata'                        Map of metadata schema/documents to add to asset
        'Identifiers'                           Map of asset identifiers
        'Preservation_files_fixity_callback'    Callback to allow external generated fixity values
        'Access_files_fixity_callback'          Callback to allow external generated fixity values
        'IO_Identifier_callback'                Callback to allow external generated Asset identifier
        'Preservation_Representation_Name'      Name of the Preservation Representation
        'Access_Representation_Name'            Name of the Access Representation
    """
    # some basic validation
    if export_folder is None:
        export_folder = tempfile.gettempdir()
    if not os.path.isdir(export_folder):
        logger.error("Export Folder Does Not Exist")
        raise RuntimeError(export_folder, "Export Folder Does Not Exist")
    if parent_folder is None:
        logger.error("You must specify a parent folder for the package asset")
        raise RuntimeError("You must specify a parent folder for the package asset")

    io_ref = None
    xip = None
    default_asset_title = None
    preservation_refs_dict = {}
    access_refs_dict = {}

    security_tag = kwargs.get('SecurityTag', "open")
    content_type = kwargs.get('CustomType', "")

    if not compress:
        shutil.register_archive_format("szip", _make_stored_zipfile, None, "UnCompressed ZIP file")

    has_preservation_files = bool((preservation_files_list is not None) and (len(preservation_files_list) > 0))
    has_access_files = bool((access_files_list is not None) and (len(access_files_list) > 0))

    if has_preservation_files:
        if default_asset_title is None:
            default_asset_title = os.path.splitext(os.path.basename(preservation_files_list[0]))[0]

        # create the asset
        xip, io_ref = __create_io__(file_name=default_asset_title, parent_folder=parent_folder, **kwargs)

    if has_access_files:
        if default_asset_title is None:
            default_asset_title = os.path.splitext(os.path.basename(access_files_list[0]))[0]

        if io_ref is None:
            xip, io_ref = __create_io__(file_name=default_asset_title, parent_folder=parent_folder, **kwargs)

    if has_preservation_files:
        # add the content objects
        representation_name = kwargs.get('Preservation_Representation_Name', "Preservation")
        preservation_refs_dict = __make_representation_multiple_co__(xip, rep_name=representation_name,
                                                                     rep_type="Preservation",
                                                                     rep_files=preservation_files_list, io_ref=io_ref)

    if has_access_files:
        # add the content objects
        access_name = kwargs.get('Access_Representation_Name', "Access")
        access_refs_dict = __make_representation_multiple_co__(xip, rep_name=access_name, rep_type="Access",
                                                               rep_files=access_files_list, io_ref=io_ref)

    if has_preservation_files:

        for content_ref, filename in preservation_refs_dict.items():
            default_content_objects_title = os.path.splitext(os.path.basename(filename))[0]
            preservation_content_title = kwargs.get('Preservation_Content_Title', default_content_objects_title)
            preservation_content_description = kwargs.get('Preservation_Content_Description',
                                                          default_content_objects_title)

            if isinstance(preservation_content_title, dict):
                preservation_content_title = preservation_content_title[filename]

            if isinstance(preservation_content_description, dict):
                preservation_content_description = preservation_content_description[filename]

            __make_content_objects__(xip, preservation_content_title, content_ref, io_ref, security_tag,
                                     preservation_content_description, content_type)

    if has_access_files:

        for content_ref, filename in access_refs_dict.items():
            default_content_objects_title = os.path.splitext(os.path.basename(filename))[0]

            access_content_title = kwargs.get('Access_Content_Title', default_content_objects_title)
            access_content_description = kwargs.get('Access_Content_Description', default_content_objects_title)

            if isinstance(access_content_title, dict):
                access_content_title = access_content_title[filename]

            if isinstance(access_content_description, dict):
                access_content_title = access_content_title[filename]

            __make_content_objects__(xip, access_content_title, content_ref, io_ref, security_tag,
                                     access_content_description, content_type)

    if has_preservation_files:

        preservation_generation_label = kwargs.get('Preservation_Generation_Label', "")

        for content_ref, filename in preservation_refs_dict.items():
            preservation_file_name = os.path.basename(filename)
            __make_generation__(xip, preservation_file_name, content_ref, preservation_generation_label)

    if has_access_files:

        access_generation_label = kwargs.get('Access_Generation_Label', "")

        for content_ref, filename in access_refs_dict.items():
            access_file_name = os.path.basename(filename)
            __make_generation__(xip, access_file_name, content_ref, access_generation_label)

    if has_preservation_files:

        if 'Preservation_files_fixity_callback' in kwargs:
            callback = kwargs.get('Preservation_files_fixity_callback')
        else:
            callback = Sha1FixityCallBack()

        for content_ref, filename in preservation_refs_dict.items():
            preservation_file_name = os.path.basename(filename)
            __make_bitstream__(xip, preservation_file_name, filename, callback)

    if has_access_files:

        if 'Access_files_fixity_callback' in kwargs:
            callback = kwargs.get('Access_files_fixity_callback')
        else:
            callback = Sha1FixityCallBack()

        for content_ref, filename in access_refs_dict.items():
            access_file_name = os.path.basename(filename)
            __make_bitstream__(xip, access_file_name, filename, callback)

    if 'Identifiers' in kwargs:
        identifier_map = kwargs.get('Identifiers')
        for identifier_key, identifier_value in identifier_map.items():
            if identifier_key:
                if identifier_value:
                    identifier = SubElement(xip, 'Identifier')
                    id_type = SubElement(identifier, "Type")
                    id_type.text = identifier_key
                    id_value = SubElement(identifier, "Value")
                    id_value.text = identifier_value
                    id_io = SubElement(identifier, "Entity")
                    id_io.text = io_ref

    if 'Asset_Metadata' in kwargs:
        metadata_map = kwargs.get('Asset_Metadata')
        for metadata_ns, metadata_path in metadata_map.items():
            if metadata_ns:
                if metadata_path:
                    if os.path.exists(metadata_path) and os.path.isfile(metadata_path):
                        descriptive_metadata = xml.etree.ElementTree.parse(source=metadata_path)
                        metadata = SubElement(xip, 'Metadata', {'schemaUri': metadata_ns})
                        metadata_ref = SubElement(metadata, 'Ref')
                        metadata_ref.text = str(uuid.uuid4())
                        entity = SubElement(metadata, 'Entity')
                        entity.text = io_ref
                        content = SubElement(metadata, 'Content')
                        content.append(descriptive_metadata.getroot())
                    elif isinstance(metadata_path, str):
                        try:
                            descriptive_metadata = xml.etree.ElementTree.fromstring(metadata_path)
                            metadata = SubElement(xip, 'Metadata', {'schemaUri': metadata_ns})
                            metadata_ref = SubElement(metadata, 'Ref')
                            metadata_ref.text = str(uuid.uuid4())
                            entity = SubElement(metadata, 'Entity')
                            entity.text = io_ref
                            content = SubElement(metadata, 'Content')
                            content.append(descriptive_metadata)
                        except RuntimeError:
                            logging.info(f"Could not parse asset metadata in namespace {metadata_ns}")

    if xip is not None:
        export_folder = export_folder
        top_level_folder = os.path.join(export_folder, io_ref)
        os.mkdir(top_level_folder)
        inner_folder = os.path.join(top_level_folder, io_ref)
        os.mkdir(inner_folder)
        os.mkdir(os.path.join(inner_folder, "content"))
        metadata_path = os.path.join(inner_folder, "metadata.xml")
        metadata = open(metadata_path, "wt", encoding='utf-8')
        metadata.write(prettify(xip))
        metadata.close()
        for content_ref, filename in preservation_refs_dict.items():
            src_file = filename
            dst_file = os.path.join(os.path.join(inner_folder, "content"), os.path.basename(filename))
            shutil.copyfile(src_file, dst_file)
        for content_ref, filename in access_refs_dict.items():
            src_file = filename
            dst_file = os.path.join(os.path.join(inner_folder, "content"), os.path.basename(filename))
            shutil.copyfile(src_file, dst_file)
        if compress:
            shutil.make_archive(top_level_folder, 'zip', top_level_folder)
        else:
            shutil.make_archive(top_level_folder, 'szip', top_level_folder)
        shutil.rmtree(top_level_folder)
        return top_level_folder + ".zip"


def simple_asset_package(preservation_file=None, access_file=None, export_folder=None, parent_folder=None,
                         compress=True, **kwargs):
    """
            Create a Preservica package containing a single Asset from a single preservation file
            and an optional access file.
            The Asset contains one Content Object for each representation.

            If only the preservation file is provided the asset has one representation


            :param str preservation_file: Path to the preservation file
            :param str access_file: Path to the access file
            :param str export_folder: The package location folder
            :param Folder parent_folder: The folder to ingest the asset into
            :param bool compress: Compress the ZIP file
            :param str Title: Asset Title
            :param str Description: Asset Description
            :param str SecurityTag: Asset SecurityTag
            :param str CustomType: Asset CustomType
            :param str Preservation_Content_Title: Title of the Preservation Representation Content Object
            :param str Preservation_Content_Description: Description of the Preservation Representation Content Object
            :param str Access_Content_Title: Title of the Access Representation Content Object
            :param str Access_Content_Description: Description of the Access Representation Content Object
            :param dict Asset_Metadata: Dictionary of Asset metadata documents
            :param dict Identifiers: Dictionary of Asset rd party identifiers




    """

    # some basic validation
    if export_folder is None:
        export_folder = tempfile.gettempdir()
    if not os.path.isdir(export_folder):
        logger.error("Export Folder Does Not Exist")
        raise RuntimeError(export_folder, "Export Folder Does Not Exist")
    if parent_folder is None:
        logger.error("You must specify a parent folder for the package asset")
        raise RuntimeError("You must specify a parent folder for the package asset")

    preservation_file_list = list()
    access_file_list = list()

    if preservation_file is not None:
        preservation_file_list.append(preservation_file)

    if access_file is not None:
        access_file_list.append(access_file)

    return complex_asset_package(preservation_files_list=preservation_file_list, access_files_list=access_file_list,
                                 export_folder=export_folder, parent_folder=parent_folder, compress=compress, **kwargs)


def upload_config():
    return transfer_config


def _unpad(s):
    return s[:-ord(s[len(s) - 1:])]


class UploadAPI(AuthenticatedAPI):

    def ingest_tweet(self, twitter_user=None, tweet_id: int = 0, twitter_consumer_key=None,
                     twitter_secret_key=None, folder=None, callback=None, **kwargs):

        """
            Ingest tweets from a twitter stream by twitter username

            :param tweet_id:
            :param str twitter_user: Twitter Username
            :param str twitter_consumer_key: Optional asset title
            :param str twitter_secret_key: Optional asset description
            :param str folder: Folder to ingest into
            :param callback callback: Optional upload progress callback
            :raises RuntimeError:


        """

        def get_image(m, has_video_element):
            media_url_https_ = m["media_url_https"]
            if media_url_https_:
                req = requests.get(media_url_https_)
                if req.status_code == requests.codes.ok:
                    if has_video_element:
                        image_name_ = f"{{{media_id_str}}}_[{twitter_user}]_thumb.jpg"
                    else:
                        image_name_ = f"{{{media_id_str}}}_[{twitter_user}].jpg"
                    image_name_document_ = open(image_name_, "wb")
                    image_name_document_.write(req.content)
                    image_name_document_.close()
                    return image_name_

        def get_video(m):
            video_info_ = m["video_info"]
            variants_ = video_info_["variants"]
            for v_ in variants_:
                video_url_ = v_["url"]
                req = requests.get(video_url_)
                if req.status_code == requests.codes.ok:
                    video_name_ = f"{{{media_id_str}}}_[{twitter_user}].mp4"
                    video_name_document_ = open(video_name_, "wb")
                    video_name_document_.write(req.content)
                    video_name_document_.close()
                    return video_name_, True

        entity_client = pyPreservica.EntityAPI(username=self.username, password=self.password, server=self.server,
                                               tenant=self.tenant)
        if hasattr(folder, "reference"):
            folder = entity_client.folder(folder.reference)
        else:
            folder = entity_client.folder(folder)
        try:
            import tweepy
            from tweepy import TweepError
        except ImportError:
            logger.error("Package tweepy is required for twitter harvesting. pip install --upgrade tweepy")
            raise RuntimeError("Package tweepy is required for twitter harvesting. pip install --upgrade tweepy")
        config = configparser.ConfigParser()
        config.read('credentials.properties')
        if twitter_consumer_key is None:
            twitter_consumer_key = os.environ.get('TWITTER_CONSUMER_KEY')
            if twitter_consumer_key is None:
                try:
                    twitter_consumer_key = config['credentials']['TWITTER_CONSUMER_KEY']
                except KeyError:
                    logger.error("No valid TWITTER_CONSUMER_KEY found in method arguments, "
                                 "environment variables or credentials.properties file")
                    raise RuntimeError("No valid TWITTER_CONSUMER_KEY found in method arguments, "
                                       "environment variables or credentials.properties file")
        if twitter_secret_key is None:
            twitter_secret_key = os.environ.get('TWITTER_SECRET_KEY')
            if twitter_secret_key is None:
                try:
                    twitter_secret_key = config['credentials']['TWITTER_SECRET_KEY']
                except KeyError:
                    logger.error("No valid TWITTER_SECRET_KEY found in method arguments, "
                                 "environment variables or credentials.properties file")
                    raise RuntimeError("No valid TWITTER_SECRET_KEY found in method arguments, "
                                       "environment variables or credentials.properties file")

        api = None
        try:
            auth = tweepy.AppAuthHandler(twitter_consumer_key, twitter_secret_key)
            api = tweepy.API(auth, wait_on_rate_limit=True)
        except TweepError:
            logger.error("No valid Twitter API keys. Could not authenticate")
            raise RuntimeError("No valid Twitter API keys. Could not authenticate")
        if api is not None:
            logger.debug(api)
            tweet = api.get_status(tweet_id, tweet_mode="extended", include_entities=True)
            created_at = tweet.created_at
            id_str = tweet.id_str
            author = tweet.author.name
            tweet_entities = tweet.entities
            hashtags = dict()
            if 'hashtags' in tweet_entities:
                hashtags = tweet.entities['hashtags']
            entities = entity_client.identifier("tweet_id", id_str.strip())
            if len(entities) > 0:
                logger.warning("Tweet already exists, skipping....")
                return
            logger.info(f"Processing tweet {id_str} ...")
            tid = tweet.id
            content_objects = list()
            full_tweet = api.get_status(tid, tweet_mode="extended", include_entities=True)
            text = tweet.full_text
            full_text = full_tweet.full_text
            file_name = f"{{{id_str}}}_[{twitter_user}].json"
            json_doc = json.dumps(full_tweet._json)
            json_file = open(file_name, "wt", encoding="utf-8")
            json_file.write(json_doc)
            json_file.close()
            content_objects.append(file_name)
            if hasattr(full_tweet, "extended_entities"):
                extended_entities = full_tweet.extended_entities
                if "media" in extended_entities:
                    media = extended_entities["media"]
                    for med in media:
                        media_id_str = med["id_str"]
                        has_video = False
                        if "video_info" in med:
                            co, has_video = get_video(med)
                            content_objects.append(co)
                            if has_video:
                                co = get_image(med, has_video)
                                content_objects.append(co)
                            continue
                        if "media_url_https" in med:
                            co = get_image(med, has_video)
                            content_objects.append(co)
            identifiers = dict()
            asset_metadata = dict()
            identifiers["tweet_id"] = id_str

            user = full_tweet._json['user']

            if full_tweet._json.get('retweeted_status'):
                retweeted_status = full_tweet._json['retweeted_status']
                if retweeted_status.get("extended_entities"):
                    extended_entities = retweeted_status["extended_entities"]
                    if "media" in extended_entities:
                        media = extended_entities["media"]
                        for med in media:
                            media_id_str = med["id_str"]
                            has_video = False
                            if "video_info" in med:
                                co, has_video = get_video(med)
                                content_objects.append(co)
                                continue
                            if "media_url_https" in med:
                                co = get_image(med, has_video)
                                content_objects.append(co)

            xml_object = xml.etree.ElementTree.Element('tweet', {"xmlns": "http://www.preservica.com/tweets/v1"})
            xml.etree.ElementTree.SubElement(xml_object, "id").text = id_str
            xml.etree.ElementTree.SubElement(xml_object, "full_text").text = full_text
            xml.etree.ElementTree.SubElement(xml_object, "created_at").text = str(created_at)
            xml.etree.ElementTree.SubElement(xml_object, "screen_name_sender").text = user.get('screen_name')
            for h in hashtags:
                xml.etree.ElementTree.SubElement(xml_object, "hashtag").text = str(h['text'])

            xml.etree.ElementTree.SubElement(xml_object, "name").text = author
            xml.etree.ElementTree.SubElement(xml_object, "retweet").text = str(full_tweet._json['retweet_count'])
            xml.etree.ElementTree.SubElement(xml_object, "likes").text = str(full_tweet._json['favorite_count'])

            xml_request = xml.etree.ElementTree.tostring(xml_object, encoding='utf-8')

            metadata_document = open("metadata.xml", "wt", encoding="utf-8")
            metadata_document.write(xml_request.decode("utf-8"))
            metadata_document.close()

            asset_metadata["http://www.preservica.com/tweets/v1"] = "metadata.xml"

            security_tag = kwargs.get("SecurityTag", "open")
            asset_title = kwargs.get("Title", text)
            asset_description = kwargs.get("Description", full_text)

            p = complex_asset_package(preservation_files_list=content_objects, parent_folder=folder,
                                      Title=asset_title, Description=asset_description, CustomType="Tweet",
                                      Identifiers=identifiers, Asset_Metadata=asset_metadata,
                                      SecurityTag=security_tag)
            self.upload_zip_package(p, folder=folder, callback=callback)
            for ob in content_objects:
                os.remove(ob)
            os.remove("metadata.xml")

    def ingest_twitter_feed(self, twitter_user=None, num_tweets: int = 25, twitter_consumer_key=None,
                            twitter_secret_key=None, folder=None, callback=None, **kwargs):

        """
            Ingest tweets from a twitter stream by twitter username

            :param str twitter_user: Twitter Username
            :param int num_tweets: The number of tweets from the stream
            :param str twitter_consumer_key: Optional asset title
            :param str twitter_secret_key: Optional asset description
            :param str folder: Folder to ingest into
            :param callback callback: Optional upload progress callback
            :raises RuntimeError:


        """

        def get_image(m, has_video_element):
            media_url_https_ = m["media_url_https"]
            if media_url_https_:
                req = requests.get(media_url_https_)
                if req.status_code == requests.codes.ok:
                    if has_video_element:
                        image_name_ = f"{{{media_id_str}}}_[{twitter_user}]_thumb.jpg"
                    else:
                        image_name_ = f"{{{media_id_str}}}_[{twitter_user}].jpg"
                    image_name_document_ = open(image_name_, "wb")
                    image_name_document_.write(req.content)
                    image_name_document_.close()
                    return image_name_

        def get_video(m):
            video_info_ = m["video_info"]
            variants_ = video_info_["variants"]
            for v_ in variants_:
                video_url_ = v_["url"]
                req = requests.get(video_url_)
                if req.status_code == requests.codes.ok:
                    video_name_ = f"{{{media_id_str}}}_[{twitter_user}].mp4"
                    video_name_document_ = open(video_name_, "wb")
                    video_name_document_.write(req.content)
                    video_name_document_.close()
                    return video_name_, True

        entity_client = pyPreservica.EntityAPI(username=self.username, password=self.password, server=self.server,
                                               tenant=self.tenant)
        if hasattr(folder, "reference"):
            folder = entity_client.folder(folder.reference)
        else:
            folder = entity_client.folder(folder)
        try:
            import tweepy
            #from tweepy import TweepError
        except ImportError:
            logger.error("Package tweepy is required for twitter harvesting. pip install --upgrade tweepy")
            raise RuntimeError("Package tweepy is required for twitter harvesting. pip install --upgrade tweepy")
        config = configparser.ConfigParser()
        config.read('credentials.properties')
        if twitter_consumer_key is None:
            twitter_consumer_key = os.environ.get('TWITTER_CONSUMER_KEY')
            if twitter_consumer_key is None:
                try:
                    twitter_consumer_key = config['credentials']['TWITTER_CONSUMER_KEY']
                except KeyError:
                    logger.error("No valid TWITTER_CONSUMER_KEY found in method arguments, "
                                 "environment variables or credentials.properties file")
                    raise RuntimeError("No valid TWITTER_CONSUMER_KEY found in method arguments, "
                                       "environment variables or credentials.properties file")
        if twitter_secret_key is None:
            twitter_secret_key = os.environ.get('TWITTER_SECRET_KEY')
            if twitter_secret_key is None:
                try:
                    twitter_secret_key = config['credentials']['TWITTER_SECRET_KEY']
                except KeyError:
                    logger.error("No valid TWITTER_SECRET_KEY found in method arguments, "
                                 "environment variables or credentials.properties file")
                    raise RuntimeError("No valid TWITTER_SECRET_KEY found in method arguments, "
                                       "environment variables or credentials.properties file")

        api = None
        try:
            auth = tweepy.AppAuthHandler(twitter_consumer_key, twitter_secret_key)
            api = tweepy.API(auth, wait_on_rate_limit=True)
        except TweepError:
            logger.error("No valid Twitter API keys. Could not authenticate")
            raise RuntimeError("No valid Twitter API keys. Could not authenticate")
        if api is not None:
            logger.debug(api)
            for tweet in tweepy.Cursor(api.user_timeline, id=twitter_user).items(int(num_tweets)):
                created_at = tweet.created_at
                id_str = tweet.id_str
                author = tweet.author.name
                tweet_entities = tweet.entities
                hashtags = dict()
                if 'hashtags' in tweet_entities:
                    hashtags = tweet.entities['hashtags']
                entities = entity_client.identifier("tweet_id", id_str.strip())
                if len(entities) > 0:
                    logger.warning("Tweet already exists, skipping....")
                    continue
                logger.info(f"Processing tweet {id_str} ...")
                tid = tweet.id
                content_objects = list()
                full_tweet = api.get_status(tid, tweet_mode="extended", include_entities=True)
                text = tweet.text
                full_text = full_tweet.full_text
                file_name = f"{{{id_str}}}_[{twitter_user}].json"
                json_doc = json.dumps(full_tweet._json)
                json_file = open(file_name, "wt", encoding="utf-8")
                json_file.write(json_doc)
                json_file.close()
                content_objects.append(file_name)
                if hasattr(full_tweet, "extended_entities"):
                    extended_entities = full_tweet.extended_entities
                    if "media" in extended_entities:
                        media = extended_entities["media"]
                        for med in media:
                            media_id_str = med["id_str"]
                            has_video = False
                            if "video_info" in med:
                                co, has_video = get_video(med)
                                content_objects.append(co)
                                if has_video:
                                    co = get_image(med, has_video)
                                    content_objects.append(co)
                                continue
                            if "media_url_https" in med:
                                co = get_image(med, has_video)
                                content_objects.append(co)
                identifiers = {}
                asset_metadata = {}
                identifiers["tweet_id"] = id_str

                user = full_tweet._json['user']

                if full_tweet._json.get('retweeted_status'):
                    retweeted_status = full_tweet._json['retweeted_status']
                    if retweeted_status.get("extended_entities"):
                        extended_entities = retweeted_status["extended_entities"]
                        if "media" in extended_entities:
                            media = extended_entities["media"]
                            for med in media:
                                media_id_str = med["id_str"]
                                has_video = False
                                if "video_info" in med:
                                    co, has_video = get_video(med)
                                    content_objects.append(co)
                                    continue
                                if "media_url_https" in med:
                                    co = get_image(med, has_video)
                                    content_objects.append(co)

                xml_object = xml.etree.ElementTree.Element('tweet', {"xmlns": "http://www.preservica.com/tweets/v1"})
                xml.etree.ElementTree.SubElement(xml_object, "id").text = id_str
                xml.etree.ElementTree.SubElement(xml_object, "full_text").text = full_text
                xml.etree.ElementTree.SubElement(xml_object, "created_at").text = str(created_at)
                xml.etree.ElementTree.SubElement(xml_object, "screen_name_sender").text = user.get('screen_name')
                for h in hashtags:
                    xml.etree.ElementTree.SubElement(xml_object, "hashtag").text = str(h['text'])

                xml.etree.ElementTree.SubElement(xml_object, "name").text = author
                xml.etree.ElementTree.SubElement(xml_object, "retweet").text = str(full_tweet._json['retweet_count'])
                xml.etree.ElementTree.SubElement(xml_object, "likes").text = str(full_tweet._json['favorite_count'])

                xml_request = xml.etree.ElementTree.tostring(xml_object, encoding='utf-8')

                metadata_document = open("metadata.xml", "wt", encoding="utf-8")
                metadata_document.write(xml_request.decode("utf-8"))
                metadata_document.close()

                asset_metadata["http://www.preservica.com/tweets/v1"] = "metadata.xml"

                security_tag = kwargs.get("SecurityTag", "open")
                asset_title = kwargs.get("Title", text)
                asset_description = kwargs.get("Description", full_text)

                p = complex_asset_package(preservation_files_list=content_objects, parent_folder=folder,
                                          Title=asset_title, Description=asset_description, CustomType="Tweet",
                                          Identifiers=identifiers, Asset_Metadata=asset_metadata,
                                          SecurityTag=security_tag)
                self.upload_zip_package(p, folder=folder, callback=callback)
                for ob in content_objects:
                    os.remove(ob)
                os.remove("metadata.xml")
                sleep(2)

    def ingest_web_video(self, url=None, parent_folder=None, **kwargs):
        """
            Ingest a web video such as YouTube etc based on the URL

            :param str url: URL to the youtube video
            :param Folder parent_folder: The folder to ingest the video into
            :param str Title: Optional asset title
            :param str Description: Optional asset description
            :param str SecurityTag: Optional asset security tag
            :param dict Identifiers: Optional asset 3rd party identifiers
            :param dict Asset_Metadata: Optional asset additional descriptive metadata
            :param callback callback: Optional upload progress callback
            :raises RuntimeError:


        """
        try:
            import youtube_dl
        except ImportError:
            logger.error("Package youtube_dl is required for this method. pip install --upgrade youtube-dl")
            raise RuntimeError("Package youtube_dl is required for this method. pip install --upgrade youtube-dl")

        ydl_opts = {}

        def my_hook(d):
            if d['status'] == 'finished':
                logger.info('Download Complete. Uploading to Preservica ...')

        ydl_opts = {
            'outtmpl': '%(id)s.mp4',
            'progress_hooks': [my_hook],
        }

        # if True:
        #    ydl_opts['writesubtitles'] = True
        #    ydl_opts['writeautomaticsub'] = True
        #    ydl_opts['subtitleslangs'] = ['en']

        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            meta = ydl.extract_info(url, download=True)

            vid_id = meta.get('id')

            if 'Title' in kwargs:
                title = kwargs.get("Title")
            else:
                title = meta.get('title')

            if 'Description' in kwargs:
                description = kwargs.get("Description")
            else:
                description = meta.get('description')

            if 'SecurityTag' in kwargs:
                security_tag = kwargs.get("SecurityTag")
            else:
                security_tag = "open"

            if 'Identifiers' in kwargs:
                identifier_map = kwargs.get('Identifiers')
                identifier_map["Video-ID"] = vid_id
            else:
                identifier_map = {"Video-ID": vid_id}

            if 'Asset_Metadata' in kwargs:
                descriptive_metadata = kwargs.get('Asset_Metadata')
            else:
                descriptive_metadata = {}

            if 'callback' in kwargs:
                callback = kwargs.get("callback")
            else:
                callback = None

            upload_date = meta.get('upload_date')
            duration = meta.get('duration')

            package = simple_asset_package(preservation_file=f"{vid_id}.mp4", parent_folder=parent_folder, Title=title,
                                           Description=description, Identifiers=identifier_map,
                                           Asset_Metadata=descriptive_metadata,
                                           Preservation_Content_Title=title, SecurityTag=security_tag)

            self.upload_zip_package(path_to_zip_package=package, folder=parent_folder, callback=callback)

    def __convert_(self, key, cypher_text):
        base64_decoded = base64.b64decode(cypher_text)
        key = base64.b64decode(self.version_hash.encode("utf-8") + key.encode("UTF-8")).decode("utf-8").encode("utf-8")
        aes = cryptography.hazmat.primitives.ciphers.algorithms.AES(key)
        cipher = Cipher(algorithm=aes, mode=modes.ECB())
        decryptor = cipher.decryptor()
        output_bytes = decryptor.update(base64_decoded) + decryptor.finalize()
        return _unpad(output_bytes.decode("utf-8")).strip()

    def upload_buckets(self):
        """
        Get a list of available upload buckets

        :return: dict of bucket names and regions
        """
        request = self.session.get(f"https://{self.server}/api/admin/locations/upload",
                                   auth=HTTPBasicAuth(self.username, self.password))

        buckets = {}
        xml_tag = "N2YxcGVsUA=="
        if request.status_code == requests.codes.ok:
            xml_response = str(request.content.decode('utf-8'))
            entity_response = xml.etree.ElementTree.fromstring(xml_response)
            data_sources = entity_response.findall('.//dataSource')
            for data_source in data_sources:
                sip_locations = data_source.findall('.//sipLocation')
                for sip_location in sip_locations:
                    if sip_location.attrib['region'] == "":
                        buckets[self.__convert_(xml_tag, sip_location.text)] = "Unknown"
                    else:
                        buckets[self.__convert_(xml_tag, sip_location.text)] = self.__convert_(xml_tag,
                                                                                               sip_location.attrib[
                                                                                                   'region'])
        return buckets

    """
    def ingest_folder_structure(self, folder_path, bucket_name, parent_folder, callback=None,
                                security_tag: str = "open",
                                delete_after_upload=True, max_MB_ingested: int = -1):

        def get_parent(client, code, parent_ref):
            identifier = str(os.path.dirname(code))
            if not identifier:
                identifier = code
            entities = client.identifier("code", identifier)
            if len(entities) > 0:
                folder = entities.pop()
                folder = client.folder(folder.reference)
                return folder.reference
            else:
                return parent

        def get_folder(client, name, security_tag, parent_ref, code):
            entities = client.identifier("code", code)
            if len(entities) == 0:
                logger.info(f"Creating new folder with name {name}")
                folder = client.create_folder(name, name, security_tag, parent_ref)
                client.add_identifier(folder, "code", code)
            else:
                logger.info(f"Found existing folder with name {name}")
                folder = entities.pop()
            return folder

        from pyPreservica import EntityAPI
        entity_client = EntityAPI()

        if parent_folder:
            parent = entity_client.folder(parent_folder)
            logger.info(f"Folders will be created inside Preservica collection {parent.title}")
            parent = parent.reference
        else:
            parent = None

        bytes_ingested = 0

        folder_path = os.path.normpath(folder_path)

        for dirname, subdirs, files in os.walk(folder_path):
            base = os.path.basename(dirname)
            code = os.path.relpath(dirname, Path(folder_path).parent)
            f = get_folder(base, security_tag, get_parent(code, parent), code)
            identifiers = dict()
            for file in list(files):
                full_path = os.path.join(dirname, file)
                if os.path.islink(full_path):
                    logger.info(f"Skipping link {file}")
                    files.remove(file)
                    continue
                asset_code = os.path.join(code, file)
                if len(entity_client.identifier("code", asset_code)) == 0:
                    bytes_ingested = bytes_ingested + os.stat(full_path).st_size
                    logger.info(f"Adding new file: {file} to package ready for upload")
                    file_identifiers = {"code": asset_code}
                    identifiers[full_path] = file_identifiers
                else:
                    logger.info(f"Skipping file {file} already exists in repository")
                    files.remove(file)

            if len(files) > 0:
                full_path_list = [os.path.join(dirname, file) for file in files]
                package = multi_asset_package(asset_file_list=full_path_list, parent_folder=f, SecurityTag=security_tag,
                                              Identifiers=identifiers)
                self.upload_zip_package_to_S3(path_to_zip_package=package, bucket_name=bucket_name,
                                              callback=callback, delete_after_upload=delete_after_upload)
                logger.info(f"Uploaded " + "{:.1f}".format(bytes_ingested / (1024 * 1024)) + " MB")

                if max_MB_ingested > 0:
                    if bytes_ingested > (1024 * 1024 * max_MB_ingested):
                        logger.info(f"Reached Max Upload Limit")
                        break
    """

    def upload_zip_package_to_Azure(self, path_to_zip_package, container_name, folder=None, delete_after_upload=False,
                                    show_progress=False):

        """
         Uploads a zip file package to an Azure container connected to a Preservica Cloud System

         :param str path_to_zip_package: Path to the package
         :param str container_name: container connected to an ingest workflow
         :param Folder folder: The folder to ingest the package into
         :param bool delete_after_upload: Delete the local copy of the package after the upload has completed

        """

        from azure.storage.blob import ContainerClient

        request = requests.get(f"https://{self.server}/api/admin/locations/upload?refresh={container_name}",
                               auth=HTTPBasicAuth(self.username, self.password))

        if request.status_code is not requests.codes.ok:
            raise SystemError(request.content)
        if request.status_code == requests.codes.ok:
            xml_response = str(request.content.decode('utf-8'))
            entity_response = xml.etree.ElementTree.fromstring(xml_response)
            a = entity_response.find('.//a')
            b = entity_response.find('.//b')
            c = entity_response.find('.//c')
            t = entity_response.find('.//type')
            xml_tag = "N2YxcGVsUA=="
            account_key = self.__convert_(xml_tag, a.text)
            session_token = self.__convert_(xml_tag, c.text)
            access_type = self.__convert_(xml_tag, t.text)

            sas_url = f"https://{account_key}.blob.core.windows.net/{container_name}?{session_token}"
            container = ContainerClient.from_container_url(sas_url)

            upload_key = str(uuid.uuid4())
            metadata = {'key': upload_key, 'name': upload_key + ".zip", 'bucket': container_name, 'status': 'ready'}

            if hasattr(folder, "reference"):
                metadata['collectionreference'] = folder.reference
            elif isinstance(folder, str):
                metadata['collectionreference'] = folder

            properties = None

            len_bytes = Path(path_to_zip_package).stat().st_size

            if show_progress:
                with tqdm.wrapattr(open(path_to_zip_package, 'rb'), "read", total=len_bytes) as data:
                    blob_client = container.upload_blob(name=upload_key, data=data, metadata=metadata, length=len_bytes)
                    properties = blob_client.get_blob_properties()
            else:
                with open(path_to_zip_package, "rb") as data:
                    blob_client = container.upload_blob(name=upload_key, data=data, metadata=metadata, length=len_bytes)
                    properties = blob_client.get_blob_properties()

            if delete_after_upload:
                os.remove(path_to_zip_package)

            return properties

    def upload_zip_package_to_S3(self, path_to_zip_package, bucket_name, folder=None, callback=None,
                                 delete_after_upload=False):

        """
         Uploads a zip file package to an S3 bucket connected to a Preservica Cloud System

         :param str path_to_zip_package: Path to the package
         :param str bucket_name: Bucket connected to an ingest workflow
         :param Folder folder: The folder to ingest the package into
         :param Callable callback: Optional callback to allow the callee to monitor the upload progress
         :param bool delete_after_upload: Delete the local copy of the package after the upload has completed

        """

        request = requests.get(f"https://{self.server}/api/admin/locations/upload?refresh={bucket_name}",
                               auth=HTTPBasicAuth(self.username, self.password))

        if request.status_code is not requests.codes.ok:
            raise SystemError(request.content)
        if request.status_code == requests.codes.ok:
            xml_response = str(request.content.decode('utf-8'))
            entity_response = xml.etree.ElementTree.fromstring(xml_response)
            a = entity_response.find('.//a')
            b = entity_response.find('.//b')
            c = entity_response.find('.//c')
            xml_tag = "N2YxcGVsUA=="
            access_key = self.__convert_(xml_tag, a.text)
            secret_key = self.__convert_(xml_tag, b.text)
            session_token = self.__convert_(xml_tag, c.text)

            session = boto3.Session(aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                                    aws_session_token=session_token)
            s3 = session.resource(service_name="s3")

            upload_key = str(uuid.uuid4())
            s3_object = s3.Object(bucket_name, upload_key)
            metadata = {'key': upload_key, 'name': upload_key + ".zip", 'bucket': bucket_name, 'status': 'ready'}

            if hasattr(folder, "reference"):
                metadata['collectionreference'] = folder.reference
            elif isinstance(folder, str):
                metadata['collectionreference'] = folder

            metadata['size'] = str(Path(path_to_zip_package).stat().st_size)
            metadata['createdby'] = self.username

            metadata_map = {'Metadata': metadata}

            s3_object.upload_file(path_to_zip_package, Callback=callback, ExtraArgs=metadata_map,
                                  Config=transfer_config)

            if delete_after_upload:
                os.remove(path_to_zip_package)

    def upload_zip_package(self, path_to_zip_package, folder=None, callback=None, delete_after_upload=False):
        """
        Uploads a zip file package directly to Preservica and starts an ingest workflow

        :param str path_to_zip_package: Path to the package
        :param Folder folder: The folder to ingest the package into
        :param Callable callback: Optional callback to allow the callee to monitor the upload progress
        :param bool delete_after_upload: Delete the local copy of the package after the upload has completed

        :return: preservica-progress-token to allow the workflow progress to be monitored
        :rtype: str


        :raises RuntimeError:


        """
        bucket = f'{self.tenant.lower()}.package.upload'
        endpoint = f'https://{self.server}/api/s3/buckets'
        self.token = self.__token__()

        s3_client = boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=self.token,
                                 aws_secret_access_key="NOT_USED",
                                 config=Config(s3={'addressing_style': 'path'}))

        metadata = {}
        if folder is not None:
            if hasattr(folder, "reference"):
                metadata = {'Metadata': {'structuralobjectreference': folder.reference}}
            elif isinstance(folder, str):
                metadata = {'Metadata': {'structuralobjectreference': folder}}

        if os.path.exists(path_to_zip_package) and os.path.isfile(path_to_zip_package):
            try:
                key_id = str(uuid.uuid4()) + ".zip"

                transfer = S3Transfer(client=s3_client, config=transfer_config)

                transfer.PutObjectTask = PutObjectTask
                transfer.CompleteMultipartUploadTask = CompleteMultipartUploadTask
                transfer.upload_file = upload_file

                response = transfer.upload_file(self=transfer, filename=path_to_zip_package, bucket=bucket, key=key_id,
                                                extra_args=metadata, callback=callback)

                if delete_after_upload:
                    os.remove(path_to_zip_package)

                return response['ResponseMetadata']['HTTPHeaders']['preservica-progress-token']

            except ClientError as e:
                logger.error(e)
                raise e
