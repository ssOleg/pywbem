#!/usr/bin/env python
"""
Test the complete PyWBEM client.

Approach:

* The HTTPretty package is used to intercept the client communication at the
  socket level. It catches PyWBEM requests going out and produces corresponding
  responses coming back in.

* Because HTTPretty seems to be able to register only one response for a
  particular URL, each test case can perform only one WBEM request and in its
  setup the test case needs to register the desired response for that request
  with HTTPretty.

* The testcases then performs the request at the PyWBEM client API, and
  verifies the response also at the level of the PyWBEM client API.

* HTTPretty remembers the lest request that was sent, so after issuing the
  request, the CIM-XML produced by the PyWBEM client is verified based on that.
"""

import os
import inspect
import doctest
import socket
import unittest
import pytest

import yaml
import httpretty
from lxml import etree, doctestcompare

import pywbem

# Directory with the JSON test case files, relative to this script:
TESTCASE_DIR = os.path.join(os.path.dirname(__file__), "test_client")


class ClientTestError(Exception):
    """Exception indicating an issue in the test case definition."""
    pass


def obj(value, tc_name):
    """Create a PyWBEM CIM object from a testcase value.

    Examples for a testcase value (JSON object / Python dict):

    1. CIMInstance object:

        {
            "pywbem_object": "CIMInstance",
            "classname": "PyWBEM_Person",
            "properties": {
                "Name": "Fritz",
                "Address": "Fritz Town"
            },
            "path": {
                "pywbem_object": "CIMInstanceName",
                "classname": "PyWBEM_Person",
                "namespace": "root/cimv2",
                "keybindings": {
                    "Name": "Fritz"
                }
            }
        }

    2. boolean object:

        False

    """

    if isinstance(value, dict) and "pywbem_object" in value:
        ctor_name = value["pywbem_object"]
        try:
            ctor_call = getattr(pywbem, ctor_name)
        except AttributeError:
            raise ClientTestError("Error in test case %s: Unknown type "\
                                  "specified in 'pywbem_object' attribute: %s"%\
                                  (tc_name, ctor_name))
        ctor_args = {}
        for arg_name in value:
            if arg_name == "pywbem_object":
                continue
            ctor_args[arg_name] = obj(value[arg_name], tc_name)
        obj_ = ctor_call(**ctor_args)
    else:
        obj_ = value
    return obj_


def tc_getattr(tc_name, dict_, key, default=-1):
    try:
        value = dict_[key]
    except KeyError:
        if default != -1:
            return default
        raise ClientTestError("Error in test case %s: '%s' attribute missing"%\
                              (tc_name, key))
    return value


def tc_hasattr(dict_, key):
    return key in dict_

class Callback(object):
    """A class with static methods that are HTTPretty callback functions
    for raising expected exceptions at the socket level."""

    @staticmethod
    def socket_ssl(request, uri, headers):
        """HTTPretty callback function that raises an arbitrary
        socket.sslerror."""
        raise socket.sslerror(1, "Arbitrary SSL error.")

    @staticmethod
    def socket_104(request, uri, headers):
        """HTTPretty callback function that raises socket.error 104."""
        raise socket.error(104, "Connection reset by peer.")

    @staticmethod
    def socket_32(request, uri, headers):
        """HTTPretty callback function that raises socket.error 32."""
        raise socket.error(32, "Broken pipe.")


class ClientTest(unittest.TestCase):
    """Test case for PyWBEM client testing."""

    def assertXMLEqual(self, s1, s2, entity=None):
        """Assert that the two XML fragments are equal, tolerating the following
        variations:
          * whitespace outside of element content and attribute values.
          * order of attributes.
          * order of certain child elements (see `sort_elements` in this
            function).

        Parameters:
          * s1 and s2 are string representations of an XML fragment.
        """

        parser = etree.XMLParser(remove_blank_text=True)
        x1 = etree.XML(s1, parser=parser)
        x2 = etree.XML(s2, parser=parser)

        # Sort certain elements

        def sort_children(root, sort_elements):
            for tag, attr in sort_elements:
                # elems is a list of elements with this tag name
                elems = root.xpath("//*[local-name() = $tag]", tag=tag)
                if len(elems) > 0:
                    parent = elems[0].getparent()
                    first = None
                    after = None
                    for i in range(0, len(parent)):
                        if parent[i].tag == tag and first is None:
                            first = i
                        if parent[i].tag != tag and first is not None:
                            after = i
                    # The following changes the original XML tree:
                    parent[first:after] = sorted(elems,
                                                 key=lambda e: e.attrib[attr])

        sort_elements = [
            # Sort sibling elements with <first> tag by its <second> attribute
            ("IPARAMVALUE", "NAME"),
            ("PROPERTY", "NAME"),
            ("PARAMETER", "NAME"),
        ]
        sort_children(x1, sort_elements)
        sort_children(x2, sort_elements)

        ns1 = etree.tostring(x1)
        ns2 = etree.tostring(x2)

        checker = doctestcompare.LXMLOutputChecker()
        # This tolerates differences in whitespace and attribute order
        if not checker.check_output(ns1, ns2, 0):
            diff = checker.output_difference(doctest.Example("", ns1), ns2, 0)
            raise AssertionError("XML is not as expected in %s: %s"%\
                                 (entity, diff))

    def test_all(self):
        """This method performs all tests of this script. It iterates through
        the JSON files in the test case directory and processes each of them.
        """

        print "" # We are in the middle of test runner output

        # We need to work with absolute file paths, because this test may be
        # run from a different directory.
        for basefn in os.listdir(TESTCASE_DIR):
            relfn = os.path.join(TESTCASE_DIR, basefn)
            if relfn.endswith(".yaml"):
                self.runyamlfile(relfn)

    def runyamlfile(self, fn):
        """Read a YAML test case file and process each test case it defines.
        """

        print "Processing YAML file: %s" % os.path.basename(fn)

        with open(fn) as fp:
            testcases = yaml.load(fp)
            for testcase in testcases:
                self.runtestcase(testcase)

    @httpretty.activate
    def runtestcase(self, testcase):
        """Run a single test case."""

        tc_name = tc_getattr("", testcase, "name")
        tc_desc = tc_getattr(tc_name, testcase, "description", None)

        print "Processing test case: %s: %s" % (tc_name, tc_desc)

        pywbem_request = tc_getattr(tc_name, testcase, "pywbem_request")
        exp_http_request = tc_getattr(tc_name, testcase, "http_request", None)
        http_response = tc_getattr(tc_name, testcase, "http_response", None)
        exp_pywbem_response = tc_getattr(tc_name, testcase,
                                         "pywbem_response")

        # Setup HTTPretty for one WBEM operation
        if exp_http_request is not None:
            exp_http_exception = tc_getattr(tc_name, http_response, "exception",
                                            None)
            if exp_http_exception is None:
                params = {
                    "body": tc_getattr(tc_name, http_response, "data"),
                    "adding_headers": tc_getattr(tc_name, http_response,
                                                 "headers", None),
                    "status": tc_getattr(tc_name, http_response, "status")
                }
            else:
                callback_name = exp_http_exception
                try:
                    callback_func = getattr(Callback(), callback_name)
                except AttributeError:
                    raise ClientTestError("Error in testcase %s: Unknown "\
                                          "exception callback specified: %s"%\
                                          (tc_name, callback_name))
                params = {
                    "body": callback_func
                }
            httpretty.register_uri(
                method=tc_getattr(tc_name, exp_http_request, "verb"),
                uri=tc_getattr(tc_name, exp_http_request, "url"),
                **params)

        conn = pywbem.WBEMConnection(
            url=tc_getattr(tc_name, pywbem_request, "url"),
            creds=tc_getattr(tc_name, pywbem_request, "creds"),
            default_namespace=tc_getattr(tc_name, pywbem_request,
                                         "namespace"),
            timeout=tc_getattr(tc_name, pywbem_request, "timeout"))

        conn.debug = tc_getattr(tc_name, pywbem_request, "debug", False)

        op = tc_getattr(tc_name, pywbem_request, "operation")
        # Example:
        #  "operation": {
        #      "pywbem_method": "GetInstance",
        #      "InstanceName": {
        #          "pywbem_object": "CIMInstanceName",
        #          "classname": "PyWBEM_Person",
        #          "keybindings": {
        #              "Name": "Fritz"
        #          }
        #      },
        #      "LocalOnly": False
        #  }

        op_name = tc_getattr(tc_name, op, "pywbem_method")
        op_args = {}
        for arg_name in op:
            if arg_name == "pywbem_method":
                continue
            op_args[arg_name] = obj(op[arg_name], tc_name)
        try:
            op_call = getattr(conn, op_name)
        except AttributeError as exc:
            raise ClientTestError("Error in testcase %s: Unknown "\
                                  "operation name: %s" %\
                                  (tc_name, op_name))

        # Invoke the PyWBEM operation to be tested
        try:
            result = op_call(**op_args)
            raised_exception = None
        except Exception as exc:
            raised_exception = exc
            result = None

        # Validate HTTP request produced by PyWBEM

        if exp_http_request is not None:
            http_request = httpretty.last_request()
            exp_verb = tc_getattr(tc_name, exp_http_request, "verb")
            self.assertEqual(http_request.method, exp_verb,
                             "verb in HTTP request")
            exp_headers = tc_getattr(tc_name, exp_http_request, "headers", {})
            for header_name in exp_headers:
                self.assertEqual(http_request.headers[header_name],
                                 exp_headers[header_name],
                                 "headers in HTTP request")
            exp_data = tc_getattr(tc_name, exp_http_request, "data", None)
            self.assertXMLEqual(http_request.body, exp_data,
                                "data in HTTP request")

        # Validate PyWBEM result

        exp_exception = tc_getattr(tc_name, exp_pywbem_response,
                                   "exception", None)
        exp_cim_status = tc_getattr(tc_name, exp_pywbem_response,
                                    "cim_status", 0)
        exp_result = tc_getattr(tc_name, exp_pywbem_response, "result",
                                None)
        if exp_exception is not None and exp_result is not None:
            raise ClientTestError("Error in testcase %s: 'result' and "\
                                  "'exception' attributes in "\
                                  "'pywbem_result' are not compatible." %\
                                  tc_name)
        if exp_cim_status != 0 and exp_result is not None:
            raise ClientTestError("Error in testcase %s: 'result' and "\
                                  "'cim_status' attributes in "\
                                  "'pywbem_result' are not compatible." %\
                                  tc_name)

        if exp_exception is not None:
            if raised_exception is None:
                raise AssertionError("%s exception was expected to be raised, "\
                                     "but no exception was actually raised." %\
                                     exp_exception)
            self.assertEqual(raised_exception.__class__.__name__,
                             exp_exception, "expected PyWBEM exception name")
        elif exp_cim_status != 0:
            if raised_exception is None:
                raise AssertionError("CIMError exception was expected to be "\
                                     "raised, but no exception was actually "
                                     "raised.")
            self.assertEqual(raised_exception.__class__.__name__, "CIMError",
                             "expected PyWBEM exception name")
        else:
            if raised_exception is not None:
                raise AssertionError("No exception was expected to be raised, "\
                                     "but %s exception was actually raised: "\
                                     "%s" %\
                                     (raised_exception.__class__.__name__,
                                      raised_exception))

        if isinstance(raised_exception, pywbem.CIMError):
            cim_status = raised_exception[0]
        else:
            cim_status = 0
        self.assertEqual(cim_status, exp_cim_status, "PyWBEM CIM status")

        if exp_result is not None:
            exp_result_obj = obj(exp_result, tc_name)
            if result != exp_result_obj:
                print "Details for the following assertion error:"
                print "- Expected result:"
                print "  Returned object: %r" % exp_result_obj
                if hasattr(exp_result_obj, "properties"):
                    print "  Its properties: %r" % exp_result_obj.properties
                print "- Actual result:"
                print "  Returned object: %r" % result
                if hasattr(result, "properties"):
                    print "  Its properties: %r" % result.properties
                if conn.debug:
                    print "  HTTP response data: %r" % conn.last_raw_reply
                raise AssertionError("PyWBEM CIM result is not as expected.")
        else:
            self.assertEqual(result, None, "PyWBEM CIM result")


if __name__ == '__main__':
    unittest.main()