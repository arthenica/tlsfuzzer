# Author: George Pantelakis, (c) 2025
# Released under Gnu GPL v2.0, see LICENSE file for details

from __future__ import print_function
import traceback
import sys
import getopt
from itertools import chain
from random import sample

from tlsfuzzer.runner import Runner
from tlsfuzzer.messages import Connect, ClientHelloGenerator, \
        ClientKeyExchangeGenerator, ChangeCipherSpecGenerator, \
        FinishedGenerator, ApplicationDataGenerator, AlertGenerator, \
        split_message, PopMessageFromList, TCPBufferingEnable, \
        TCPBufferingDisable, TCPBufferingFlush
from tlsfuzzer.expect import ExpectServerHello, ExpectCertificate, \
        ExpectServerHelloDone, ExpectChangeCipherSpec, ExpectFinished, \
        ExpectAlert, ExpectApplicationData, ExpectClose, \
        ExpectServerKeyExchange


from tlslite.constants import CipherSuite, AlertLevel, AlertDescription, \
        GroupName, ExtensionType, HashAlgorithm, SignatureAlgorithm, \
        SignatureScheme
from tlslite.extensions import SupportedGroupsExtension, \
        SignatureAlgorithmsExtension, SignatureAlgorithmsCertExtension
from tlsfuzzer.utils.lists import natural_sort_keys
from tlsfuzzer.helpers import AutoEmptyExtension, sig_algs_to_ids, \
    cipher_suite_to_id


version = 2


def help_msg():
    print("Usage: <script-name> [-h hostname] [-p port] [[probe-name] ...]")
    print(" -h hostname    name of the host to run the test against")
    print("                localhost by default")
    print(" -p port        port number to use for connection, 4433 by default")
    print(" probe-name     if present, will run only the probes with given")
    print("                names and not all of them, e.g \"sanity\"")
    print(" -e probe-name  exclude the probe from the list of the ones run")
    print("                may be specified multiple times")
    print(" -x probe-name  expect the probe to fail. When such probe passes")
    print("                despite being marked like this it will be reported")
    print("                in the test summary and the whole script will fail.")
    print("                May be specified multiple times.")
    print(" -X message     expect the `message` substring in exception raised")
    print("                during execution of preceding expected failure")
    print("                probe")
    print("                usage: [-x probe-name] [-X exception], order is")
    print(" -S sigalgs     compulsory! hash and signature algorithm pairs that")
    print("                the client will accept for TLS negotiation")
    print(" -n num         run 'num' or all(if 0) tests instead of default(all)")
    print("                (\"sanity\" tests are always executed)")
    print(" -C ciph        Use specified ciphersuite. Either numerical value or")
    print("                IETF name. By default uses")
    print("                TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA, ")
    print("                TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA, and")
    print("                TLS_DHE_RSA_WITH_AES_128_CBC_SHA ciphersuites")
    print("                See tlslite.constants.CipherSuite for ciphersuite")
    print("                definitions")
    print(" -g kex         Key exchange groups to advertise in the supported_groups")
    print("                extension, separated by colons. By default:")
    print("                \"secp256r1:ffdhe2048\"")
    print(" -M | --ems     Advertise support for Extended Master Secret")
    print(" --help         this message")

def main():
    host = "localhost"
    port = 4433
    num_limit = None
    run_exclude = set()
    expected_failures = {}
    last_exp_tmp = None
    ciphers = None
    ems = False
    groups = None
    dhe = False
    customGroupsSet = False

    sig_algs = [
        (HashAlgorithm.sha512, SignatureAlgorithm.rsa),
        (HashAlgorithm.sha384, SignatureAlgorithm.rsa),
        (HashAlgorithm.sha256, SignatureAlgorithm.rsa),
        (HashAlgorithm.sha512, SignatureAlgorithm.ecdsa),
        (HashAlgorithm.sha384, SignatureAlgorithm.ecdsa),
        (HashAlgorithm.sha256, SignatureAlgorithm.ecdsa),
        SignatureScheme.ed25519,
        SignatureScheme.ed448
    ]

    argv = sys.argv[1:]
    opts, args = getopt.getopt(argv, "h:p:e:x:X:S:n:C:Mg:", ["help", "ems"])
    for opt, arg in opts:
        if opt == '-h':
            host = arg
        elif opt == '-p':
            port = int(arg)
        elif opt == '-e':
            run_exclude.add(arg)
        elif opt == '-x':
            expected_failures[arg] = None
            last_exp_tmp = str(arg)
        elif opt == '-X':
            if not last_exp_tmp:
                raise ValueError("-x has to be specified before -X")
            expected_failures[last_exp_tmp] = str(arg)
        elif opt == '-S':
            sig_algs = sig_algs_to_ids(arg)
        elif opt == '-n':
            num_limit = int(arg)
        elif opt == '-C':
            ciphers = [cipher_suite_to_id(arg)]
        elif opt == '-g':
            vals = arg.split(":")
            groups = [getattr(GroupName, i) for i in vals]
            customGroupsSet = True
        elif opt == '-M' or opt == '--ems':
            ems = True
        elif opt == '--help':
            help_msg()
            sys.exit(0)
        else:
            raise ValueError("Unknown option: {0}".format(opt))

    if args:
        run_only = set(args)
    else:
        run_only = None

    if ciphers:
        # by default send minimal set of extensions, but allow user
        # to override it
        dhe = ciphers[0] in CipherSuite.ecdhAllSuites or \
              ciphers[0] in CipherSuite.dhAllSuites
    else:
        dhe = True
        ciphers = [CipherSuite.TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA,
                   CipherSuite.TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA,
                   CipherSuite.TLS_DHE_RSA_WITH_AES_128_CBC_SHA]

    if groups is None:
        groups = [GroupName.secp256r1,
                  GroupName.ffdhe2048]

    CH_bytes = (
        63 +  # Base message
        (len(ciphers) * 2) +  # 2 bytes for each ciphersuite added
        (len(sig_algs) * 4) +  # 4 bytes for each signature algo added
        (len(groups) * 2) +  # 2 bytes for each group added
        (4 if ems else 0)  # 4 bytes if ems extension is added
    )
    CH_fragments = (CH_bytes // 2) + (CH_bytes % 2)

    conversations = {}

    conversation = Connect(host, port)
    node = conversation
    ext = {}
    if ems:
        ext[ExtensionType.extended_master_secret] = AutoEmptyExtension()
    ext[ExtensionType.supported_groups] = SupportedGroupsExtension()\
        .create(groups)
    ext[ExtensionType.signature_algorithms] = \
        SignatureAlgorithmsExtension().create(sig_algs)
    ext[ExtensionType.signature_algorithms_cert] = \
        SignatureAlgorithmsCertExtension().create(sig_algs)
    node = node.add_child(ClientHelloGenerator(
        ciphers + [CipherSuite.TLS_EMPTY_RENEGOTIATION_INFO_SCSV],
        extensions=ext))
    node = node.add_child(ExpectServerHello())
    node = node.add_child(ExpectCertificate())
    if dhe:
        node = node.add_child(ExpectServerKeyExchange())
    node = node.add_child(ExpectServerHelloDone())
    node = node.add_child(ClientKeyExchangeGenerator())
    node = node.add_child(ChangeCipherSpecGenerator())
    node = node.add_child(FinishedGenerator())
    node = node.add_child(ExpectChangeCipherSpec())
    node = node.add_child(ExpectFinished())
    node = node.add_child(ApplicationDataGenerator(
        bytearray(b"GET / HTTP/1.0\r\n\r\n")))
    node = node.add_child(ExpectApplicationData())
    node = node.add_child(AlertGenerator(AlertLevel.warning,
                                         AlertDescription.close_notify))
    node = node.add_child(ExpectAlert())
    node.next_sibling = ExpectClose()
    conversations["sanity"] = conversation

    conversation = Connect(host, port)
    node = conversation
    ext = {}
    if ems:
        ext[ExtensionType.extended_master_secret] = AutoEmptyExtension()
    ext[ExtensionType.supported_groups] = SupportedGroupsExtension()\
        .create(groups)
    ext[ExtensionType.signature_algorithms] = \
        SignatureAlgorithmsExtension().create(sig_algs)
    ext[ExtensionType.signature_algorithms_cert] = \
        SignatureAlgorithmsCertExtension().create(sig_algs)
    fragment_list = []
    node = node.add_child(TCPBufferingEnable())
    node = node.add_child(split_message(ClientHelloGenerator(
        ciphers, session_id=bytearray(0), extensions=ext), fragment_list, 2))
    node = node.add_child(ChangeCipherSpecGenerator(fake=True))
    node = node.add_child(TCPBufferingDisable())
    node = node.add_child(TCPBufferingFlush())
    node = node.add_child(ExpectAlert(AlertLevel.fatal,
                                      AlertDescription.unexpected_message))
    node = node.add_child(ExpectClose())
    conversations["Single interleaved ClientHello fragment"] = conversation

    conversation = Connect(host, port)
    node = conversation
    ext = {}
    if ems:
        ext[ExtensionType.extended_master_secret] = AutoEmptyExtension()
    ext[ExtensionType.supported_groups] = SupportedGroupsExtension()\
        .create(groups)
    ext[ExtensionType.signature_algorithms] = \
        SignatureAlgorithmsExtension().create(sig_algs)
    ext[ExtensionType.signature_algorithms_cert] = \
        SignatureAlgorithmsCertExtension().create(sig_algs)
    fragment_list = []
    node = node.add_child(TCPBufferingEnable())
    node = node.add_child(split_message(ClientHelloGenerator(
        ciphers, session_id=bytearray(0), extensions=ext), fragment_list, 2))
    for _ in range(CH_fragments - 1):
        node = node.add_child(ChangeCipherSpecGenerator(fake=True))
        node = node.add_child(PopMessageFromList(fragment_list))
    node = node.add_child(TCPBufferingDisable())
    node = node.add_child(TCPBufferingFlush())
    node = node.add_child(ExpectAlert(AlertLevel.fatal,
                                      AlertDescription.unexpected_message))
    node = node.add_child(ExpectClose())
    conversations["Multiple interleaved ClientHello fragment"] = conversation

    # run the conversation
    good = 0
    bad = 0
    xfail = 0
    xpass = 0
    failed = []
    xpassed = []
    if not num_limit:
        num_limit = len(conversations)

    # make sure that sanity test is run first and last
    # to verify that server was running and kept running throughout
    sanity_tests = [('sanity', conversations['sanity'])]
    if run_only:
        if num_limit > len(run_only):
            num_limit = len(run_only)
        regular_tests = [(k, v) for k, v in conversations.items() if k in run_only]
    else:
        regular_tests = [(k, v) for k, v in conversations.items() if
                         (k != 'sanity') and k not in run_exclude]
    sampled_tests = sample(regular_tests, min(num_limit, len(regular_tests)))
    ordered_tests = chain(sanity_tests, sampled_tests, sanity_tests)

    for c_name, c_test in ordered_tests:
        print("{0} ...".format(c_name))

        runner = Runner(c_test)

        res = True
        exception = None
        try:
            runner.run()
        except Exception as exp:
            exception = exp
            print("Error while processing")
            print(traceback.format_exc())
            res = False

        if c_name in expected_failures:
            if res:
                xpass += 1
                xpassed.append(c_name)
                print("XPASS-expected failure but test passed\n")
            else:
                if expected_failures[c_name] is not None and  \
                    expected_failures[c_name] not in str(exception):
                        bad += 1
                        failed.append(c_name)
                        print("Expected error message: {0}\n"
                            .format(expected_failures[c_name]))
                else:
                    xfail += 1
                    print("OK-expected failure\n")
        else:
            if res:
                good += 1
                print("OK\n")
            else:
                bad += 1
                failed.append(c_name)

    print("Reproducer for CVE-2004-0079. Checking behavior of server with")
    print("interleaved ClientHello and ChangeCipherSpec messages.")

    print("Test end")
    print(20 * '=')
    print("version: {0}".format(version))
    print(20 * '=')
    print("TOTAL: {0}".format(len(sampled_tests) + 2*len(sanity_tests)))
    print("SKIP: {0}".format(len(run_exclude.intersection(conversations.keys()))))
    print("PASS: {0}".format(good))
    print("XFAIL: {0}".format(xfail))
    print("FAIL: {0}".format(bad))
    print("XPASS: {0}".format(xpass))
    print(20 * '=')
    sort = sorted(xpassed ,key=natural_sort_keys)
    if len(sort):
        print("XPASSED:\n\t{0}".format('\n\t'.join(repr(i) for i in sort)))
    sort = sorted(failed, key=natural_sort_keys)
    if len(sort):
        print("FAILED:\n\t{0}".format('\n\t'.join(repr(i) for i in sort)))

    if bad or xpass:
        sys.exit(1)

if __name__ == "__main__":
    main()
