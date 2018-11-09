#!/usr/bin/env python

from __future__ import print_function, absolute_import

import argparse
import json
import uuid

from virtwho.virt import Guest, Hypervisor, HostGuestAssociationReport
from virtwho.config import VW_TYPES

_NUMBER_OF_HYPERVISORS = 1
_NUMBER_OF_GUESTS = 2
_DOMAIN = 'example.com'
_FACTS = {
    Hypervisor.CPU_SOCKET_FACT: "2",
    Hypervisor.HYPERVISOR_TYPE_FACT: "VMware ESXi",
    Hypervisor.HYPERVISOR_CLUSTER: "fake_cluster",
    Hypervisor.HYPERVISOR_VERSION_FACT: "6.5.0"
}


def generate_guest(virt_type, guest_state=Guest.STATE_RUNNING):
    return Guest(str(uuid.uuid4()), virt_type, guest_state)


def generate_hypervisor(guests, domain, virt_type):
    hypervisor_uuid = str(uuid.uuid4())
    hypervisor_name = "{}.{}".format(hypervisor_uuid.replace("-", ""), domain)

    guest_list = [generate_guest(virt_type) for _ in range(guests)]

    return Hypervisor(hypervisorId=hypervisor_uuid, guestIds=guest_list,
                      name=hypervisor_name, facts=_FACTS)


def generate_virtwho_report(hypervisors, guests, domain, virt_type):
    hypervisor_list = []
    for _ in range(hypervisors):
        hypervisor = generate_hypervisor(guests, domain, virt_type)
        hypervisor_list.append(hypervisor)
    data = {'hypervisors': hypervisor_list}
    return HostGuestAssociationReport({}, data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hypervisors", type=int,
        default=_NUMBER_OF_HYPERVISORS,
        help="number of hypervisors to create (default: %(default)s)")
    parser.add_argument(
        "--guests", type=int,
        default=_NUMBER_OF_GUESTS,
        help="number of guests per hypervisor to create (default: %(default)s)")
    parser.add_argument(
        "--path", type=str,
        help="file path to save the output to")
    parser.add_argument(
        "--domain", type=str,
        default=_DOMAIN,
        help="domain to place the hypervisors into (default: %(default)s)")
    parser.add_argument(
        "--virt-type", type=str,
        default='esx', choices=VW_TYPES,
        help="type of hypervisors to add (default: %(default)s,"
             " choices: %(choices)s)")
    args = parser.parse_args()

    report = generate_virtwho_report(args.hypervisors, args.guests,
                                     args.domain, args.virt_type)
    report_json = json.dumps({'hypervisors': report.hypervisors})

    if args.path:
        with open(args.path, 'w') as f:
            f.write(report_json)

    print(report_json)


if __name__ == '__main__':
    main()
