"""
This a python m3u / epg file optimizer script
This script enables download of m3u / epg files from a remote web server and introduces features to trim / optimize
these files to a set of wanted channel groups and channels along with the ability to sort / reorder channels

This can prove useful on underpowered devices where SPMC / KODI / some other app running on that device might struggle
to load a very large m3u / epg file

This script has been tested with:

1. vaderstreams m3u and epg files pulled from:
    http://<VADERSTREAMS_DOMAIN>/epg/vget?username=<USERNAME>&password=<PASSWORD>
    http://<VADERSTREAMS_DOMAIN>/p2.xml.gz

2. fab m3u and epg files pulled from:
    http://<FAB_DOMAIN>/get.php?username=<USERNAME>&password=<PASSWORD>&type=m3u_plus&output=ts
    http://<FAB_DOMAIN>/xmltv.php?username=<USERNAME>&password=<PASSWORD>
"""

import sys
import os
import argparse
import ast
import requests
import re
import shutil
import gzip
from xml.etree.cElementTree import Element, SubElement, parse, ElementTree
import datetime
import dateutil.parser
from urllib import url2pathname


class M3uItem:
    def __init__(self, m3u_fields):
        self.tvg_name = None
        self.tvg_id = None
        self.tvg_logo = None
        self.group_title = None
        self.name = None
        self.url = None
        self.sort_order = 0

        if m3u_fields is not None:
            try:
                self.tvg_name = re.search('tvg-name="(.*?)"', m3u_fields, re.IGNORECASE).group(1)
                self.tvg_id = re.search('tvg-id="(.*?)"', m3u_fields, re.IGNORECASE).group(1)
                self.tvg_logo = re.search('tvg-logo="(.*?)"', m3u_fields, re.IGNORECASE).group(1)
                self.group_title = re.search('group-title="(.*?)"', m3u_fields, re.IGNORECASE).group(1)
                self.name = m3u_fields.split(",")[1]
            except AttributeError as e:
                output_str("m3u file parse AttributeError: {0}".format(e))


class FileUriAdapter(requests.adapters.BaseAdapter):

    @staticmethod
    def chk_path(method, path):
        # type: (object, object) -> object
        if method.lower() in ('put', 'delete'):
            return 501, "Not Implemented"
        elif method.lower() not in ('get', 'head'):
            return 405, "Method Not Allowed"
        elif os.path.isdir(path):
            return 400, "Path Not A File"
        elif not os.path.isfile(path):
            return 404, "File Not Found"
        elif not os.access(path, os.R_OK):
            return 403, "Access Denied"
        else:
            return 200, "OK"

    def send(self, req, **kwargs):
        path = os.path.normcase(os.path.normpath(url2pathname(req.path_url)))
        response = requests.Response()

        response.status_code, response.reason = self.chk_path(req.method, path)
        if response.status_code == 200 and req.method.lower() != 'head':
            try:
                is_gzipped = path.lower().endswith(".gz")
                if not is_gzipped:
                    response.raw = open(path, 'rb')
                else:
                    response.raw = gzip.open(path, 'rb')
            except (OSError, IOError) as err:
                response.status_code = 500
                response.reason = str(err)

        if isinstance(req.url, bytes):
            response.url = req.url.decode('utf-8')
        else:
            response.url = req.url

        response.request = req
        response.connection = self

        return response

    def close(self):
        pass

arg_parser = argparse.ArgumentParser(description='download and optimize m3u/epg files retrieved from a remote web server', formatter_class=argparse.RawTextHelpFormatter)
arg_parser.add_argument('--m3uurl', '-m', nargs='?', help='The url to pull the m3u file from')
arg_parser.add_argument('--epgurl', '-e', nargs='?', help='The url to pull the epg file from')
arg_parser.add_argument('--groups', '-g', nargs='?', help='Channel groups in the m3u to keep')
arg_parser.add_argument('--channels', '-c', nargs='?', help='Individual channels in the m3u to discard')
arg_parser.add_argument('--range', '-r', nargs='?', help='An optional range window to consider when adding programmes to the epg')
arg_parser.add_argument('--sortchannels', '-s', nargs='?', help='The optional desired sort order for channels in the generated m3u')
arg_parser.add_argument('--outdirectory', '-d', nargs='?', help='The output folder where retrieved and generated file are to be stored')
arg_parser.add_argument('--outfilename', '-f', nargs='?', help='The output filename for the generated files')


# main entry point
def main():
    args = validate_args()

    m3u_entries = load_m3u(args)
    m3u_entries = filter_m3u_entries(args, m3u_entries)

    if m3u_entries is not None:
        if len(args.sortchannels) > 0:
            m3u_entries = sort_m3u_entries(args, m3u_entries)

        save_new_m3u(args, m3u_entries)

        epg_filename = load_epg(args)
        if epg_filename is not None:
            xml_tree = create_new_epg(args, epg_filename, m3u_entries)
            save_new_epg(args, xml_tree)


# parses and validates cli arguments passed to this script
def validate_args():
    args = arg_parser.parse_args()

    if not args.m3uurl:
        abort_process('--m3uurl is mandatory', 1)

    if not args.epgurl:
        abort_process('--epgurl is mandatory', 1)

    if not args.groups:
        abort_process('--groups is mandatory', 1)

    set_str = '([' + args.groups + '])'
    args.groups = set(ast.literal_eval(set_str))

    if args.channels:
        set_str = '([' + args.channels + '])'
        args.channels = set(ast.literal_eval(set_str))
    else:
        args.channels = set()

    if args.range:
        args.range = int(args.range)
    else:
        args.range = 168

    if args.sortchannels:
        list_str = '([' + args.sortchannels + '])'
        args.sortchannels = list(ast.literal_eval(list_str))
    else:
        args.sortchannels = []

    if not args.outdirectory:
        abort_process('--outdirectory is mandatory', 1)

    out_directory = os.path.expanduser(args.outdirectory)
    if not os.path.exists(out_directory):
        abort_process(out_directory + ' does not exist in your filesystem', 1)
    elif not os.path.isdir(out_directory):
        abort_process(out_directory + ' is not a folder in your filesystem', 1)

    if not args.outfilename:
        abort_process('--outfilename is mandatory', 1)

    return args


# controlled script abort mechanism
def abort_process(reason, exitcode):
    output_str(reason)
    sys.exit(exitcode)


# helper print function with timestamp
def output_str(event_str):
    try:
        print("%s %s" % (datetime.datetime.now().isoformat(), event_str))
    except IOError as e:
        if e.errno != 0:
            print "I/O error({0}): {1} for event string '{2}'".format(e.errno, e.strerror, event_str)


########################################################################################################################
# m3u functions
########################################################################################################################
# downloads an m3u, converts it to a list and returns it
def load_m3u(args):
    m3u_response = get_m3u(args.m3uurl)
    if m3u_response.status_code == 200:
        m3u_filename = save_original_m3u(args.outdirectory, m3u_response)
        m3u_response.close()
        m3u_entries = parse_m3u(m3u_filename)
        return m3u_entries
    else:
        m3u_response.close()


# performs the HTTP GET
def get_m3u(m3u_url):
    output_str("performing HTTP GET request to " + m3u_url)

    if m3u_url.lower().startswith('file'):
        session = requests.session()
        session.mount('file://', FileUriAdapter())
        response = session.get(m3u_url)
    else:
        response = requests.get(m3u_url)

    return response


# saves the HTTP GET response to the file system
def save_original_m3u(out_directory, m3u_response):
    m3u_target = os.path.join(out_directory, "original.m3u8")
    output_str("saving retrieved m3u file: " + m3u_target)
    with open(m3u_target, "w") as text_file:
        text_file.write(m3u_response.content)
        return m3u_target


# parses the m3u file represented by m3u_filename into a list of M3uItem objects and returns them
def parse_m3u(m3u_filename):
    output_str("parsing m3u into a list of objects")
    m3u_file = open(m3u_filename, 'r')
    line = m3u_file.readline()
    if not line.startswith('#EXTM3U'):
        return

    m3u_entries = []
    entry = M3uItem(None)

    for line in m3u_file:
        line = line.strip()
        if line.startswith('#EXTINF:'):
            m3u_fields = line.split('#EXTINF:-1 ')[1]
            entry = M3uItem(m3u_fields)
        elif len(line) != 0:
            entry.url = line
            if entry.tvg_id != "":
                m3u_entries.append(entry)
            entry = M3uItem(None)

    m3u_file.close()
    output_str("m3u contains {} items".format(len(m3u_entries)))
    return m3u_entries


# filters the given m3u_entries using the supplied groups
def filter_m3u_entries(args, m3u_entries):
    output_str("keeping channel groups in this {}".format(str(args.groups)))
    if len(args.channels) > 0:
        output_str("ignoring channels in this {}".format(str(args.channels)))

    # sort the channels by name by default
    m3u_entries = sorted(m3u_entries, key=lambda entry: entry.tvg_name)

    filtered_m3u_entries = []
    all_channels_name_target = os.path.join(args.outdirectory, "original.channels.txt")
    filtered_channels_name_target = os.path.join(args.outdirectory, args.outfilename + ".channels.txt")
    with open(all_channels_name_target, "w") as all_channels_file:
        with open(filtered_channels_name_target, "w") as filtered_channels_file:
            for m3u_entry in m3u_entries:
                if m3u_entry.group_title.lower() in args.groups and not m3u_entry.tvg_name.lower() in args.channels:
                    filtered_m3u_entries.append(m3u_entry)
                    filtered_channels_file.write("'%s','%s'\n" % (m3u_entry.tvg_name.lower(), m3u_entry.group_title.lower()))
                all_channels_file.write("'%s','%s'\n" % (m3u_entry.tvg_name.lower(), m3u_entry.group_title.lower()))

    output_str("filtered m3u contains {} items".format(len(filtered_m3u_entries)))
    return filtered_m3u_entries


# sorts the given m3u_entries using the supplied args.sortchannels
def sort_m3u_entries(args, m3u_entries):
    output_str("desired channel sort order: list{}".format(str(args.sortchannels)))
    idx = 0
    for sort_channel in args.sortchannels:
        m3u_item = next((x for x in m3u_entries if x.tvg_name.lower() == sort_channel), None)
        if m3u_item is not None:
            m3u_item.sort_order = idx
        idx += 1
    m3u_entries = sorted(m3u_entries, key=lambda entry: entry.sort_order)
    return m3u_entries


# saves the given m3u_entries into the file system
def save_new_m3u(args, m3u_entries):
    if m3u_entries is not None:
        m3u_target = os.path.join(args.outdirectory, args.outfilename + ".m3u8")
        output_str("saving new m3u file: " + m3u_target)
        with open(m3u_target, "w") as text_file:
            text_file.write("%s\n" % "#EXTM3U")
            for entry in m3u_entries:
                text_file.write('%s tvg-name="%s" tvg-id="%s" tvg-logo="%s" group-title="%s",%s\n' % ("#EXTINF:-1", entry.tvg_name, entry.tvg_id, entry.tvg_logo, entry.group_title, entry.name))
                text_file.write('%s\n' % entry.url)


########################################################################################################################
# epg functions
########################################################################################################################
# downloads an epg gzip file, saves it, extracts it and returns the path to the extracted epg xml
def load_epg(args):
    epg_response = get_epg(args.epgurl)
    if epg_response.status_code == 200:
        is_gzipped = args.epgurl.lower().endswith(".gz")
        epg_filename = save_original_epg(is_gzipped, args.outdirectory, epg_response)
        epg_response.close()
        if is_gzipped:
            epg_filename = extract_original_epg(args.outdirectory, epg_filename)
        return epg_filename
    else:
        epg_response.close()


# performs the HTTP GET
def get_epg(epg_url):
    output_str("performing HTTP GET request to " + epg_url)

    if epg_url.lower().startswith('file'):
        session = requests.session()
        session.mount('file://', FileUriAdapter())
        response = session.get(epg_url)
    else:
        response = requests.get(epg_url, stream=True)

    return response


# saves the HTTP GET response to the file system
def save_original_epg(is_gzipped, out_directory, epg_response):
    epg_target = os.path.join(out_directory, "original.gz" if is_gzipped else "original.xml")
    output_str("saving retrieved epg file: " + epg_target)

    with open(epg_target, "wb") if not isinstance(epg_response.raw, gzip.GzipFile) else gzip.open(epg_target, "wb") as epg_file:
        if not isinstance(epg_response.raw, gzip.GzipFile):
            epg_response.raw.decode_content = True
            shutil.copyfileobj(epg_response.raw, epg_file)
        else:
            epg_file.write(epg_response.content)

    return epg_target


# extracts the given epg_filename and saves it to the file system
def extract_original_epg(out_directory, epg_filename):
    epg_target = os.path.join(out_directory, "original.xml")
    output_str("extracting retrieved epg file to: " + epg_target)
    with gzip.open(epg_filename, 'rb') as f_in, open(epg_target, 'wb') as f_out:
        shutil.copyfileobj(f_in, f_out)
        return epg_target


# pretty prints the xml document represented by root_elem
def indent(root_elem, level=0):
    i = "\n" + level*"  "
    if len(root_elem):
        if not root_elem.text or not root_elem.text.strip():
            root_elem.text = i + "  "
        if not root_elem.tail or not root_elem.tail.strip():
            root_elem.tail = i
        for root_elem in root_elem:
            indent(root_elem, level + 1)
        if not root_elem.tail or not root_elem.tail.strip():
            root_elem.tail = i
    else:
        if level and (not root_elem.tail or not root_elem.tail.strip()):
            root_elem.tail = i


# returns an indicator whether the given programme in within the configured range window
def is_in_range(args, programme):
    programme_start = dateutil.parser.parse(programme.get("start"))
    now = datetime.datetime.now(programme_start.tzinfo)
    range_start = now - datetime.timedelta(hours=args.range)
    range_end = now + datetime.timedelta(hours=args.range)
    return range_start <= programme_start <= range_end


# creates a new epg from the epg represented by original_epg_filename using the given m3u_entries as a template
def create_new_epg(args, original_epg_filename, m3u_entries):
    output_str("creating new xml epg for {} m3u items".format(len(m3u_entries)))
    original_tree = parse(original_epg_filename)
    original_root = original_tree.getroot()

    new_root = Element("tv")
    new_root.set("source-info-name", "py-m3u-epg-editor")
    new_root.set("generator-info-name", "py-m3u-epg-editor")
    new_root.set("generator-info-url", "py-m3u-epg-editor")

    # create a channel element for every channel present in the m3u
    for channel in original_root.iter('channel'):
        channel_id = channel.get("id")
        if any(x.tvg_id == channel_id for x in m3u_entries):
            output_str("creating channel element for {}".format(channel_id))
            new_channel = SubElement(new_root, "channel")
            new_channel.set("id", channel_id)
            for elem in channel:
                new_elem = SubElement(new_channel, elem.tag)
                new_elem.text = elem.text
                for attr_key in elem.keys():
                    attr_val = elem.get(attr_key)
                    new_elem.set(attr_key, attr_val)

    # now copy all programme elements from the original epg for every channel present in the m3u
    no_epg_channels = []
    for entry in m3u_entries:
        if entry.tvg_id is not None and entry.tvg_id != "" and entry.tvg_id != "None":
            output_str("creating programme elements for {}".format(entry.tvg_name))
            channel_xpath = 'programme[@channel="' + entry.tvg_id + '"]'
            channel_programmes = original_tree.findall(channel_xpath)
            if len(channel_programmes) > 0:
                for elem in channel_programmes:
                    if is_in_range(args, elem):
                        programme = SubElement(new_root, elem.tag)
                        for attr_key in elem.keys():
                            attr_val = elem.get(attr_key)
                            programme.set(attr_key, attr_val)
                        for sub_elem in elem:
                            new_elem = SubElement(programme, sub_elem.tag)
                            new_elem.text = sub_elem.text
                            for attr_key in sub_elem.keys():
                                attr_val = sub_elem.get(attr_key)
                                new_elem.set(attr_key, attr_val)
            else:
                no_epg_channels.append(entry)
        else:
            no_epg_channels.append(entry)

    indent(new_root)
    tree = ElementTree(new_root)

    if len(no_epg_channels) > 0:
        save_no_epg_channels(args, no_epg_channels)

    return tree


# saves the no_epg_channels list into the file system
def save_no_epg_channels(args, no_epg_channels):
    no_epg_channels_target = os.path.join(args.outdirectory, "no_epg_channels.txt")
    with open(no_epg_channels_target, "w") as no_epg_channels_file:
        for m3u_entry in no_epg_channels:
            no_epg_channels_file.write("'%s','%s'\n" % (m3u_entry.tvg_name.lower(), m3u_entry.tvg_id))


# saves the epg xml document represented by xml_tree into the file system
def save_new_epg(args, xml_tree):
    epg_target = os.path.join(args.outdirectory, args.outfilename + ".xml")
    output_str("saving new epg xml file: " + epg_target)
    xml_tree.write(epg_target, encoding="UTF-8", xml_declaration=True)


if __name__ == '__main__':
    main()
