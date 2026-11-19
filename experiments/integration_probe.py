"""Bounded ACL probes for the isolated Issue #14 topology."""

import argparse
import json
import socket
import time

from experiments.integration_checks import ACL_SERVICES

REPLY = b"issue14"
REQUEST = b"observe"


def serve(service):
    datagram = service.protocol == "udp"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if datagram else socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((service.destination, service.port))
        if not datagram:
            listener.listen()
        print(json.dumps({"event": "ready"}), flush=True)
        while True:
            if datagram:
                data, client = listener.recvfrom(64)
                if data == REQUEST:
                    listener.sendto(REPLY, client)
            else:
                connection, _ = listener.accept()
                with connection:
                    # The connection itself constitutes TCP reachability.
                    try:
                        connection.sendall(REPLY)
                    except (BrokenPipeError, ConnectionResetError):
                        pass


def attempt(service):
    udp = service.protocol == "udp"
    connected = False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as client:
        client.settimeout(.35)
        try:
            client.connect((service.destination, service.port))
            connected = not udp
            if udp:
                client.send(REQUEST)
            return "reply" if client.recv(64) == REPLY else "unexpected_reply"
        except socket.timeout:
            return "connected_without_reply" if connected else "timeout"
        except OSError as error:
            return "socket_error:{}".format(error.errno)


def probes():
    while True:
        for service in ACL_SERVICES:
            start = time.time_ns()
            status = attempt(service)
            print(json.dumps({"protocol": service.protocol, "started_ns": start,
                              "finished_ns": time.time_ns(), "status": status}), flush=True)
        time.sleep(.2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("tcp", "udp", "probe"))
    mode = parser.parse_args().mode
    if mode == "probe":
        probes()
    else:
        serve(next(service for service in ACL_SERVICES if service.protocol == mode))


if __name__ == "__main__":
    main()
