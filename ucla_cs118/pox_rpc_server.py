# -*- Mode: python; py-indent-offset: 4; indent-tabs-mode: nil; coding: utf-8; -*-
# Copyright 2017 Alex Afanasyev (UCLA)
#
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, either version
# 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program.
# If not, see <http://www.gnu.org/licenses/>.

from pox.core import core

from pox.lib.revent import *
from pox.lib.util import str_to_bool
import pox.openflow.libopenflow_01 as of

log = core.getLogger()

import os, sys, threading, Ice, traceback

slice_dir = Ice.getSliceDir()
if not slice_dir:
    log.error(sys.argv[0] + ': Slice directory not found.')
    sys.exit(1)

Ice.loadSlice("", ["-I%s" % slice_dir, "%s/pox.ice" % os.path.dirname(os.path.realpath(__file__))])
import pox

class PacketFromSimpleRouter(Event):
  """ Fired when packet received from remote SimpleRouter """
  def __init__(self, packet, outIface):
      Event.__init__(self)
      self.packet = packet
      self.outIface = outIface

class PacketInjector(pox.PacketInjector):
    def __init__(self, communicator, rpc):
        self._communicator = communicator
        self._rpc = rpc
        self._destroy = False
        self._clients = []
        self._cond = threading.Condition()
        self._ifaces = []

    def sendPacket(self, packet, outIface, current=None):
        log.debug("Request injection of packet length %d on interface %s\n" % (len(packet), outIface))
        self._rpc.raiseEvent(PacketFromSimpleRouter(packet, outIface))

    def sendToClients(self, packet, inIface):
        log.debug("Send to client packet size %d from %s" % (len(packet), inIface))

        self._cond.acquire()
        try:
            clients = self._clients[:]
        finally:
            self._cond.release()

        if len(clients) > 0:
            for client in clients:
                try:
                    client.handlePacket(packet, inIface)
                except:
                    log.debug("removing client `" + self._communicator.identityToString(client.ice_getIdentity()) + "'")

                    self._cond.acquire()
                    try:
                        self._clients.remove(client)
                    finally:
                        self._cond.release()

    def resetToClients(self, ifaces):
        log.debug("Request requestReset with %d interfaces" % len(ifaces))
        self._ifaces = ifaces

        self._cond.acquire()
        try:
            clients = self._clients[:]
        finally:
            self._cond.release()

        if len(clients) > 0:
            for client in clients:
                try:
                    client.resetRouter(ifaces)
                except:
                    log.debug("removing client `" + self._communicator.identityToString(client.ice_getIdentity()) + "'")

                    self._cond.acquire()
                    try:
                        self._clients.remove(client)
                    finally:
                        self._cond.release()

    def addPacketHandler(self, identity, current=None):
        self._cond.acquire()
        try:
            log.debug("Got connection from: " + current.con.getEndpoint().toString() +
                      " (id: " + self._communicator.identityToString(identity) + ")")

            client = pox.PacketHandlerPrx.uncheckedCast(current.con.createProxy(identity))
            self._clients.append(client)
        finally:
            self._cond.release()

    def getIfaces(self, current=None):
        return self._ifaces

def getIfaces(ports):
    ifaces = []
    for port in ports:
        if port > 60000:
            continue
        iface = pox.Iface()
        iface.port = port
        iface.name = ports[port].name
        iface.mac = ports[port].hw_addr.toRaw()

        ifaces.append(iface)
    return ifaces

class ConnectionController(EventMixin):
    """
    Redirect packets to/from SimpleRouter
    """
    def __init__ (self, rpc, connection):
        EventMixin.__init__(self)
        self._rpc = rpc
        self._connection = connection

        self.listenTo(connection)
        self.listenTo(self._rpc)

        self._rpc._injector.resetToClients(getIfaces(self._connection.ports))

    def _handle_PacketIn(self, event):
        if (event.ofp.in_port > 60000):
            # Ignore packets from the switch
            return

        pkt = event.parse()
        raw_packet = pkt.raw

        # Should not really happen, but in case there is some a discrepancy
        inPort = self._connection.ports[event.ofp.in_port]
        if inPort is None:
            log.error("Got packet from an unknown port: %s" % event.ofp.in_port)
            return

        self._rpc._injector.sendToClients(pkt.raw, inPort.name)

    def _handle_PacketFromSimpleRouter(self, event):
        # Find interface
        try:
            port = self._connection.ports[event.outIface].port_no
        except:
            log.debug("Request to send packet to an unknown interface `%s`. Skipping" % event.outIface)
            return;
        msg = of.ofp_packet_out()
        msg.actions.append(of.ofp_action_output(port=port))
        msg.in_port = of.OFPP_NONE
        msg.data = event.packet
        self._connection.send(msg)

class SimpleRouterRpc(threading.Thread, EventMixin):
    """
    Module that talks to a remote SimpleRouter implementation
    """

    _eventMixin_events = set([
        PacketFromSimpleRouter,
        ])

    def __init__(self, controller, host, port, trace):
        threading.Thread.__init__(self)
        EventMixin.__init__(self)
        self.controller = controller
        self.host = host
        self.port = port
        self.trace = trace

    def run(self):
        # self._ic = Ice.initialize(self.initData)
        self._ic = Ice.initialize(["pox_rpc_server", "--Ice.Trace.Network=1"])
        self._adapter = self._ic.createObjectAdapterWithEndpoints("SimpleRouter", "tcp -h %s -p %s" % (self.host, self.port))
        self._injector = PacketInjector(self._ic, self)
        self._adapter.add(self._injector, self._ic.stringToIdentity("SimpleRouter"))
        self._adapter.activate()
        self._ic.waitForShutdown()

    def stop(self):
        self._ic.shutdown()
        self._ic.destroy()
        self.join(4.0)
        if self.isAlive():
            log.debug("Tried to shutdown controller, but failed")
        else:
            log.debug("Shutdown complete")

class RouterController(EventMixin):
    """
    Waits for OpenFlow switches to connect and start redirecting packets to/from simple router
    """

    def __init__(self, host, port, trace):
        """
        """
        self.listenTo(core.openflow)
        self.listenTo(core.core)
        core.openflow.miss_send_len = 65535
        log.info("Starting packet redirector...")

        self.rpc = SimpleRouterRpc(self, host, port, trace)
        self.rpc.start()

    def _handle_ConnectionUp(self, event):
        log.debug("Connection UP %s" % (event.connection,))
        ConnectionController(self.rpc, event.connection)

    def _handle_ConnectionDown (self, event):
        log.debug("Connection DOWN %s" % (event.connection,))
        self.rpc._injector.resetToClients([])

    def _handle_GoingDownEvent(self, event):
        log.info("Stopping packet redirector...")
        self.rpc.stop()

def launch(host="127.0.0.1", port="8888", trace=True):
  """
  Starts a packet redirector from/to OpenFlow switch
  """
  core.registerNew(RouterController, host, port, trace)
