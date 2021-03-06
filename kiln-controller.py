#!/usr/bin/env python

import os
import sys
import logging
import json

import bottle
import gevent
import geventwebsocket

# from bottle import post, get
from gevent.pywsgi import WSGIServer
from geventwebsocket.handler import WebSocketHandler

# aws stuff
from awscrt import io, mqtt, auth, http
from awsiot import mqtt_connection_builder
from awscrt.io import LogLevel

try:
    sys.dont_write_bytecode = True
    import config

    sys.dont_write_bytecode = False
except:
    print("Could not import config file.")
    print("Copy config.py.EXAMPLE to config.py and adapt it for your setup.")
    exit(1)

logging.basicConfig(level=config.log_level, format=config.log_format)
log = logging.getLogger("kiln-controller")
log.info("Starting kill controller")

script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, script_dir + "/lib/")
profile_path = os.path.join(script_dir, "storage", "profiles")

from oven import Oven, Profile
from ovenWatcher import OvenWatcher

app = bottle.Bottle()
oven = Oven()
ovenWatcher = OvenWatcher(oven)

io.init_logging(LogLevel.Debug, "stderr")


@app.route("/")
def index():
    return bottle.redirect("/picoreflow/index.html")


@app.post("/test")
def handle_api():
    log.info("/test is alive")
    log.info(bottle.request.json)
    event_loop_group = io.EventLoopGroup(1)
    host_resolver = io.DefaultHostResolver(event_loop_group)
    client_bootstrap = io.ClientBootstrap(event_loop_group, host_resolver)

    mqtt_connection = mqtt_connection_builder.mtls_from_path(
        endpoint=os.getenv("endpoint"),
        cert_filepath=os.getenv("cert_filepath"),
        pri_key_filepath=os.getenv("key"),
        client_bootstrap=client_bootstrap,
        ca_filepath=os.getenv("root_ca"),
        on_connection_interrupted=on_connection_interrupted,
        on_connection_resumed=on_connection_resumed,
        client_id=os.getenv("client_id"),
        clean_session=False,
        keep_alive_secs=6,
    )
    log.info(mqtt_connection)

    connect_future = mqtt_connection.connect()
    connect_future.result()
    log.info("connect")
    message = "{} [{}]".format(1, 1)

    mqtt_connection.publish(topic="test", payload=message, qos=mqtt.QoS.AT_LEAST_ONCE)

    return {"success": True}

# Callback when connection is accidentally lost.
def on_connection_interrupted(connection, error, **kwargs):
    print("Connection interrupted. error: {}".format(error))


# Callback when an interrupted connection is re-established.
def on_connection_resumed(connection, return_code, session_present, **kwargs):
    print("Connection resumed. return_code: {} session_present: {}".format(return_code, session_present))

    if return_code == mqtt.ConnectReturnCode.ACCEPTED and not session_present:
        print("Session did not persist. Resubscribing to existing topics...")
        resubscribe_future, _ = connection.resubscribe_existing_topics()

        # Cannot synchronously wait for resubscribe result because we're on the connection's event-loop thread,
        # evaluate result with a callback instead.
        resubscribe_future.add_done_callback(on_resubscribe_complete)
@app.post("/api")
def handle_api():
    log.info("/api is alive")
    log.info(bottle.request.json)

    # run a kiln schedule
    if bottle.request.json["cmd"] == "run":
        wanted = bottle.request.json["profile"]
        log.info("api requested run of profile = %s" % wanted)

        # start at a specific minute in the schedule
        # for restarting and skipping over early parts of a schedule
        startat = 0
        if "startat" in bottle.request.json:
            startat = bottle.request.json["startat"]

        # get the wanted profile/kiln schedule
        profile = find_profile(wanted)
        if profile is None:
            return {"success": False, "error": "profile %s not found" % wanted}

        # FIXME juggling of json should happen in the Profile class
        profile_json = json.dumps(profile)
        profile = Profile(profile_json)
        oven.run_profile(profile, startat=startat)
        ovenWatcher.record(profile)

    if bottle.request.json["cmd"] == "stop":
        log.info("api stop command received")
        oven.abort_run()

    return {"success": True}


def find_profile(wanted):
    """
    given a wanted profile name, find it and return the parsed
    json profile object or None.
    """
    # load all profiles from disk
    profiles = get_profiles()
    json_profiles = json.loads(profiles)

    # find the wanted profile
    for profile in json_profiles:
        if profile["name"] == wanted:
            return profile
    return None


@app.route("/picoreflow/:filename#.*#")
def send_static(filename):
    log.debug("serving %s" % filename)
    return bottle.static_file(
        filename,
        root=os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])), "public"),
    )


def get_websocket_from_request():
    env = bottle.request.environ
    wsock = env.get("wsgi.websocket")
    if not wsock:
        abort(400, "Expected WebSocket request.")
    return wsock


@app.route("/control")
def handle_control():
    wsock = get_websocket_from_request()
    log.info("websocket (control) opened")
    while True:
        try:
            message = wsock.receive()
            log.info("Received (control): %s" % message)
            msgdict = json.loads(message)
            if msgdict.get("cmd") == "RUN":
                log.info("RUN command received")
                profile_obj = msgdict.get("profile")
                if profile_obj:
                    profile_json = json.dumps(profile_obj)
                    profile = Profile(profile_json)
                oven.run_profile(profile)
                ovenWatcher.record(profile)
            elif msgdict.get("cmd") == "SIMULATE":
                log.info("SIMULATE command received")
                # profile_obj = msgdict.get('profile')
                # if profile_obj:
                #    profile_json = json.dumps(profile_obj)
                #    profile = Profile(profile_json)
                # simulated_oven = Oven(simulate=True, time_step=0.05)
                # simulation_watcher = OvenWatcher(simulated_oven)
                # simulation_watcher.add_observer(wsock)
                # simulated_oven.run_profile(profile)
                # simulation_watcher.record(profile)
            elif msgdict.get("cmd") == "STOP":
                log.info("Stop command received")
                oven.abort_run()
        except WebSocketError:
            break
    log.info("websocket (control) closed")


@app.route("/storage")
def handle_storage():
    wsock = get_websocket_from_request()
    log.info("websocket (storage) opened")
    while True:
        try:
            message = wsock.receive()
            if not message:
                break
            log.debug("websocket (storage) received: %s" % message)

            try:
                msgdict = json.loads(message)
            except:
                msgdict = {}

            if message == "GET":
                log.info("GET command received")
                wsock.send(get_profiles())
            elif msgdict.get("cmd") == "DELETE":
                log.info("DELETE command received")
                profile_obj = msgdict.get("profile")
                if delete_profile(profile_obj):
                    msgdict["resp"] = "OK"
                wsock.send(json.dumps(msgdict))
                # wsock.send(get_profiles())
            elif msgdict.get("cmd") == "PUT":
                log.info("PUT command received")
                profile_obj = msgdict.get("profile")
                # force = msgdict.get('force', False)
                force = True
                if profile_obj:
                    # del msgdict["cmd"]
                    if save_profile(profile_obj, force):
                        msgdict["resp"] = "OK"
                    else:
                        msgdict["resp"] = "FAIL"
                    log.debug("websocket (storage) sent: %s" % message)

                    wsock.send(json.dumps(msgdict))
                    wsock.send(get_profiles())
        except WebSocketError:
            break
    log.info("websocket (storage) closed")


@app.route("/config")
def handle_config():
    wsock = get_websocket_from_request()
    log.info("websocket (config) opened")
    while True:
        try:
            message = wsock.receive()
            wsock.send(get_config())
        except WebSocketError:
            break
    log.info("websocket (config) closed")


@app.route("/status")
def handle_status():
    wsock = get_websocket_from_request()
    ovenWatcher.add_observer(wsock)
    log.info("websocket (status) opened")
    while True:
        try:
            message = wsock.receive()
            wsock.send("Your message was: %r" % message)
        except WebSocketError:
            break
    log.info("websocket (status) closed")


def get_profiles():
    try:
        profile_files = os.listdir(profile_path)
    except:
        profile_files = []
    profiles = []
    for filename in profile_files:
        with open(os.path.join(profile_path, filename), "r") as f:
            profiles.append(json.load(f))
    return json.dumps(profiles)


def save_profile(profile, force=False):
    profile_json = json.dumps(profile)
    filename = profile["name"] + ".json"
    filepath = os.path.join(profile_path, filename)
    if not force and os.path.exists(filepath):
        log.error("Could not write, %s already exists" % filepath)
        return False
    with open(filepath, "w+") as f:
        f.write(profile_json)
        f.close()
    log.info("Wrote %s" % filepath)
    return True


def delete_profile(profile):
    profile_json = json.dumps(profile)
    filename = profile["name"] + ".json"
    filepath = os.path.join(profile_path, filename)
    os.remove(filepath)
    log.info("Deleted %s" % filepath)
    return True


def get_config():
    return json.dumps(
        {
            "temp_scale": config.temp_scale,
            "time_scale_slope": config.time_scale_slope,
            "time_scale_profile": config.time_scale_profile,
            "kwh_rate": config.kwh_rate,
            "currency_type": config.currency_type,
        }
    )


def main():
    ip = config.listening_ip
    port = config.listening_port
    log.info("listening on %s:%d" % (ip, port))

    server = WSGIServer((ip, port), app, handler_class=WebSocketHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
