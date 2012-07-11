#!/usr/bin/env python

import ctypes
import logging
import simplejson as json
import socket
import struct
import time

import pump
import pump_mc

import mc_bin_client
import memcacheConstants

class CBSink(pump_mc.MCSink):
    """Smart client sink to couchbase cluster."""

    def scatter_gather(self, mconns, batch):
        sink_map_buckets = self.sink_map['buckets']
        if len(sink_map_buckets) != 1:
            return "error: CBSink.run() expected 1 bucket in sink_map", None

        retry_batch = None
        vbuckets_num = len(sink_map_buckets[0]['vBucketServerMap']['vBucketMap'])
        vbuckets = batch.group_by_vbucket_id(vbuckets_num)

        # Scatter or send phase.
        for vbucket_id, msgs in vbuckets.iteritems():
            rv, conn = self.find_conn(mconns, vbucket_id)
            if rv != 0:
                return rv, None
            rv = self.send_msgs(conn, msgs, self.operation(),
                                vbucket_id=vbucket_id)
            if rv != 0:
                return rv, None

        # Yield to let other threads do stuff while server's processing.
        time.sleep(0.01)

        # Gather or recv phase.
        for vbucket_id, msgs in vbuckets.iteritems():
            rv, conn = self.find_conn(mconns, vbucket_id)
            if rv != 0:
                return rv, None
            rv, retry = self.recv_msgs(conn, msgs, vbucket_id=vbucket_id)
            if rv != 0:
                return rv, None
            if retry:
                retry_batch = batch

        return 0, retry_batch

    @staticmethod
    def can_handle(opts, spec):
        return (spec.startswith("http://") or
                spec.startswith("couchbase://"))

    @staticmethod
    def check_source(opts, source_class, source_spec, sink_class, sink_spec):
        if (source_spec.startswith("http://") or
            source_spec.startswith("couchbase://")):
            return None
        return pump.Sink.check_source(opts, source_class, source_spec,
                                      sink_class, sink_spec)

    @staticmethod
    def check(opts, spec, source_map):
        rv, sink_map = pump.rest_couchbase(opts, spec)
        if rv != 0:
            return rv, None

        rv, source_bucket_name = pump.find_source_bucket_name(opts, source_map)
        if rv != 0:
            return rv, None
        rv, sink_bucket_name = pump.find_sink_bucket_name(opts, source_bucket_name)
        if rv != 0:
            return rv, None

        # Adjust sink_map['buckets'] to have only our sink_bucket.
        sink_buckets = [bucket for bucket in sink_map['buckets']
                        if bucket['name'] == sink_bucket_name]
        if not sink_buckets:
            return "error: missing bucket-destination: " + sink_bucket_name + \
                " at destination: " + spec + \
                "; perhaps your username/password is missing or incorrect", None
        if len(sink_buckets) != 1:
            return "error: multiple buckets with name: " + sink_bucket_name + \
                " at destination: " + spec, None
        sink_map['buckets'] = sink_buckets

        return 0, sink_map

    @staticmethod
    def consume_config(opts, sink_spec, sink_map,
                       source_bucket, source_map, source_config):
        if source_config:
            logging.warn("warning: cannot restore bucket configuration"
                         " on a couchbase destination")
        return 0

    @staticmethod
    def consume_design(opts, sink_spec, sink_map,
                       source_bucket, source_map, source_design):
        if not source_design:
            return 0

        try:
            sd = json.loads(source_design)
        except ValueError as e:
            return "error: could not parse source design; exception: %s" % (e)
        if not sd:
            return "error: could not parse source design"

        if (not sink_map['buckets'] or
            len(sink_map['buckets']) != 1 or
            not sink_map['buckets'][0] or
            not sink_map['buckets'][0]['name']):
            return "error: design sink incorrect sink_map bucket"

        spec_parts = pump.parse_spec(opts, sink_spec, 8091)
        if not spec_parts:
            return "error: design sink no spec_parts: " + sink_spec

        sink_nodes = pump.filter_bucket_nodes(sink_map['buckets'][0],
                                              spec_parts)
        if not sink_nodes:
            return "error: design sink nodes missing"

        couch_api_base = sink_nodes[0].get('couchApiBase')
        if not couch_api_base:
            return "error: cannot restore bucket design" \
                " on a couchbase cluster that does not support couch API;" \
                " the couchbase cluster may be an older, pre-2.0 version;" \
                " please check your cluster URL: " + sink_spec

        host, port, user, pswd, path = \
            pump.parse_spec(opts, couch_api_base, 8092)

        for row in sd['rows']:
            logging.debug("design_doc row: " + str(row))

            id = row['id']
            logging.debug("design_doc id: " + id + " at: " + path + "/" + id)

            if '_rev' in row['doc']:
                del row['doc']['_rev']

            doc = json.dumps(row['doc'])
            logging.debug("design_doc: " + doc)

            try:
                err, conn, response = \
                    pump.rest_request(host, int(port), user, pswd,
                                      path + "/" + id, method='PUT', body=doc)
                if conn:
                    conn.close()
                if err:
                    return ("error: could not restore design doc id: %s" +
                            "; response: %s; err: %s") % (id, response, err)
            except Exception as e:
                return ("error: design sink exception: %s" +
                        "; couch_api_base: %s") % (e, couch_api_base)

            logging.debug("design_doc created at: " + path + "/" + id)

        return 0

    def find_conn(self, mconns, vbucket_id):
        bucket = self.sink_map['buckets'][0]

        vBucketMap = bucket['vBucketServerMap']['vBucketMap']
        serverList = bucket['vBucketServerMap']['serverList']

        if vbucket_id > len(vBucketMap):
            return "error: map missing vbucket_id: " + str(vbucket_id) + \
                "; perhaps your source does not have vbuckets" + \
                "; if so, try using moxi (HOST:11211) as a destination", None

        # Primary server for a vbucket_id is the 0'th entry.
        host_port = serverList[vBucketMap[vbucket_id][0]]

        conn = mconns.get(host_port, None)
        if not conn:
            host, port = host_port.split(':')
            user = bucket['name']
            pswd = bucket['saslPassword']
            rv, conn = CBSink.connect_mc(host, port, user, pswd)
            if rv != 0:
                logging.error("error: CBSink.connect() for send: " + rv)
                return rv, None
            mconns[host_port] = conn

        return 0, conn
