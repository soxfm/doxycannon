#!/usr/bin/env python2
import argparse
import docker
import glob
import re
import os
from Queue import Queue
from threading import Thread

VERSION = '0.2.0'
IMAGE = 'audibleblink/doxycannon'
THREADS = 20
START_PORT = 5000

PROXYCHAINS_CONF = './proxychains.conf'
PROXYCHAINS_TEMPLATE = """
# This file is automatically generated by doxycannon. If you need changes,
# make them to the template string in doxycannon.py
random_chain
quiet_mode
proxy_dns
remote_dns_subnet 224
tcp_read_time_out 15000
tcp_connect_time_out 8000

[ProxyList]
"""

HAPROXY_CONF = './haproxy/haproxy.cfg'
HAPROXY_TEMPLATE = """
# This file is automatically generated by doxycannon. If you need changes,
# make them to the template string in doxycannon.py
global
        daemon
        user root
        group root

defaults
        mode tcp
        maxconn 3000
        timeout connect 5000ms
        timeout client 50000ms
        timeout server 50000ms

listen funnel_proxy
        bind *:1337
        mode tcp
        balance roundrobin
        default_backend doxycannon

backend doxycannon
"""

doxy = docker.from_env()


def build(image_name, path='.'):
    """Builds the image with the given name"""
    try:
        doxy.images.build(path=path, tag=image_name)
        message = '[*] Image {} built.'
        print message.format(image_name)
    except Exception as err:
        print err
        raise


def vpn_file_queue(folder):
    """Returns a Queue of files from the given directory"""
    files = glob.glob(folder + '/*.ovpn')
    jobs = Queue(maxsize=0)
    for f in files:
        jobs.put(f)
    return jobs


def write_config(filename, data, conf_type):
    """ Write data to a given filename

    The `type` argument determines what template gets written
    at the beginning of the config file. Types are either
    'haproxy' or 'proxychains'
    """
    with open(filename, 'w') as f:
        if conf_type == 'haproxy':
            f.write(HAPROXY_TEMPLATE)
        elif conf_type == 'proxychains':
            f.write(PROXYCHAINS_TEMPLATE)
        for line in data:
            f.write(line + "\n")


def write_haproxy_conf(port_range):
    """Generates HAProxy config based on # of ovpn files"""
    print "[+] Writing HAProxy configuration"
    conf_line = "\tserver doxy{} 127.0.0.1:{} check"
    data = list(map(lambda x: conf_line.format(x, x), port_range))
    write_config(HAPROXY_CONF, data, 'haproxy')


def write_proxychains_conf(port_range):
    """Generates Proxychains4 config based on # of ovpn files"""
    print "[+] Writing Proxychains4 configuration"
    conf_line = "socks5 127.0.0.1 {}"
    data = list(map(lambda x: conf_line.format(x), port_range))
    write_config(PROXYCHAINS_CONF, data, 'proxychains')


def containers_from_image(image_name):
    """Returns a Queue of containers whose source image match image_name"""
    jobs = Queue(maxsize=0)
    containers = list(
        filter(
            lambda x: image_name in x.attrs['Config']['Image'],
            doxy.containers.list()
        )
    )
    for container in containers:
        jobs.put(container)
    return jobs


def multikill(jobs):
    """Handler to job killer. Called by the Thread worker function."""
    while True:
        container = jobs.get()
        print 'Stopping: {}'.format(container.name)
        container.kill(9)
        jobs.task_done()


def down(image_name):
    """Find all containers from an image name and start workers for them.
    The workers are tasked with running the job killer function
    """
    container_queue = containers_from_image(image_name)
    for _ in range(THREADS):
        worker = Thread(target=multikill, args=(container_queue,))
        worker.setDaemon(True)
        worker.start()
    container_queue.join()
    print '[+] All containers have been issued a kill commaand'


def multistart(image_name, jobs, ports):
    """Handler for starting containers. Called by Thread worker function."""
    while True:
        port = ports.get()
        ovpn_basename = os.path.basename(jobs.get())
        ovpn_stub = re.sub("\.ovpn", "", ovpn_basename)
        print 'Starting: {}'.format(ovpn_stub)
        doxy.containers.run(
            image_name,
            auto_remove=True,
            privileged=True,
            ports={'1080/tcp': ('127.0.0.1', port)},
            dns=['1.1.1.1'],
            environment=["VPN={}".format(ovpn_stub)],
            name=ovpn_stub,
            detach=True)
        port = port + 1
        jobs.task_done()


def start_containers(image_name, ovpn_queue, port_range):
    """Starts workers that call the container creation function"""
    port_queue = Queue(maxsize=0)
    for p in port_range:
        port_queue.put(p)

    for _ in range(THREADS):
        worker = Thread(
            target=multistart,
            args=(image_name, ovpn_queue, port_queue,))
        worker.setDaemon(True)
        worker.start()
    ovpn_queue.join()
    print '[+] All containers have been issued a start command'


def up(image):
    """Kick off the `up` process that starts all the containers

    Writes the configuration files and starts starts container based
    on the number of *.ovpn files in the VPN folder
    """
    ovpn_file_queue = vpn_file_queue('./VPN')
    ovpn_file_count = len(list(ovpn_file_queue.queue))
    port_range = range(START_PORT, START_PORT + ovpn_file_count)
    write_haproxy_conf(port_range)
    write_proxychains_conf(port_range)
    start_containers(image, ovpn_file_queue, port_range)


def single(image):
    """Starts an HAProxy rotator.

    Builds and starts the HAProxy container in the haproxy folder
    This will create a local socks5 proxy on port 1337 that will
    allow one to configure applications with SOCKS proxy options.
    Ex: Firefox, BurpSuite, etc.
    """
    import signal
    import sys

    name = 'doxyproxy'

    def signal_handler(*args):
        """Traps ctrl+c for cleanup, then exits"""
        sys.stdout = open(os.devnull, 'w')
        down(name)
        sys.stdout = sys.__stdout__
        print '\n[*] {} was issued a stop command'.format(name)
        print '[*] Your proxies are still running.'
        sys.exit(0)

    try:
        if not list(containers_from_image(image).queue):
            up(image)
        else:
            ovpn_file_count = len(list(vpn_file_queue('VPN').queue))
            port_range = range(START_PORT, START_PORT + ovpn_file_count)
            write_haproxy_conf(port_range)
        build(name, path='./haproxy')
        print '[*] Staring single-port mode. Ctrl-c to quit'
        signal.signal(signal.SIGINT, signal_handler)
        doxy.containers.run(name, network='host', name=name, auto_remove=True)
    except Exception as err:
        print err
        raise


def interactive(image):
    """Starts the interactive process. Requires Proxychains4

    Creates a shell session where network connections are routed through
    proxychains. Started GUI application from here rarely works
    """
    try:
        if not list(containers_from_image(image).queue):
            up(image)
        else:
            ovpn_file_count = len(list(vpn_file_queue('VPN').queue))
            port_range = range(START_PORT, START_PORT + ovpn_file_count)
            write_proxychains_conf(port_range)

        os.system("proxychains4 bash")
    except Exception as err:
        print err
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--build',
        action='store_true',
        default=False,
        dest='build',
        help='Builds the base docker image')
    parser.add_argument(
        '--up',
        action='store_true',
        default=False,
        dest='up',
        help='Brings up containers. 1 for each VPN file in ./VPN')
    parser.add_argument(
        '--down',
        action='store_true',
        default=False,
        dest='down',
        help='Bring down all the containers')
    parser.add_argument(
        '--single',
        action='store_true',
        default=False,
        dest='single',
        help='Start an HAProxy rotator on a single port. Useful for Burpsuite')
    parser.add_argument(
        '--interactive',
        action='store_true',
        default=False,
        dest='interactive',
        help="Starts an interactive bash session where network connections" +
        " are routed through proxychains. Requires proxychainvs v4+")
    parser.add_argument(
        '--version',
        action='version',
        version="%(prog)s {}".format(VERSION))
    args = parser.parse_args()

    if args.build:
        build(IMAGE)
    elif args.up:
        up(IMAGE)
    elif args.down:
        down(IMAGE)
    elif args.interactive:
        interactive(IMAGE)
    elif args.single:
        single(IMAGE)


if __name__ == "__main__":
    main()
