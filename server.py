import os
import json

# FIXME : not sure why but docker cloud only displays
#         stderr in its logs ..
import sys
import logging

import etcd
import dockercloud
from dockercloud.api.events import Events

logging.basicConfig(level=logging.DEBUG)

dockercloud.user = os.environ.get('DOCKERCLOUD_USER');
dockercloud.apikey = os.environ.get('DOCKERCLOUD_APIKEY');

if not dockercloud.user or not dockercloud.apikey:
    raise Exception('DOCKERCLOUD_USER && DOCKERCLOUD_APIKEY environment variables must be specified')

infra_stack = os.environ.get('STACK_ENV')

if not infra_stack:
    infra_stack = 'infra'

etcd_hostname = 'etcd.' + infra_stack
etcd_client = etcd.Client(host=etcd_hostname) # FIXME : protocol https?

event_manager = Events()

def on_open():
    logging.warning('Connection inited with docker cloud api')

def on_close():
    logging.warning('Shutting down')

def get_container(message):
    uri = message.get('resource_uri').split('/')[-2]
    return dockercloud.Container.fetch(uri)

def get_envvar(container, to_find):
    for envvar in container.container_envvars:
        if envvar['key'] == to_find:
            return envvar['value']
    return None

def get_container_hostname(container):
    hostname = container.name
    stack = get_envvar(container, 'DOCKERCLOUD_STACK_NAME')
    if stack:
       hostname = '%s.%s' % (hostname, stack)
    return hostname

# -------------------------------------------------------------------------

def insert(key, value, message):
    try:
        etcd_client.read(key)
        logging.warning(key + ' already exists')
        return True
    except etcd.EtcdKeyNotFound:
        etcd_client.write(key, value)
        logging.warning(message)
        return False

def remove(key, message):
    try:
      etcd_client.delete(key)
      logging.warning(message)
      return True
    except etcd.EtcdKeyNotFound as e:
      logging.error(e)
      return False

# -------------------------------------------------------------------------

def create_backend(backend_name):
    key = '/vulcand/backends/%s/backend' % backend_name
    value = '{"Type": "http"}' # FIXME : https

    return insert(key, value, 'Created backend : %s' % key)

def create_frontend(backend_name, ROUTE):
    key = '/vulcand/frontends/%s/frontend' % backend_name
    # NOTE : Route could be passed as a raw string.
    #        More flexible but not needed
    value = '{"Type": "http", "BackendId": "%s", "Route": "PathRegexp(`%s.*`)"}'\
            % (backend_name, ROUTE) # FIXME : https
 
    return insert(key, value, 'Created frontend : %s' % key)

def create_server(container, backend_name, server_name, ROUTE, PORT):
    HOSTNAME = get_container_hostname(container)

    key = '/vulcand/backends/%s/servers/%s' % (backend_name, server_name)
    value = '{"URL": "http://%s:%s"}' % (HOSTNAME, PORT)

    # Once the service frontend was created we can change the servers as we want
    # That's why we don't use insert,
    # Change the server is essential to be able to change versions
    # or simply relaunch a container that failed
    etcd_client.write(key, value)
    logging.warning('Added server: %s = %s on route %s' % (key, value, ROUTE))

# -------------------------------------------------------------------------

def add_https_redirect(backend_name):
    key = '/vulcand/frontends/%s/middlewares/http2https' % backend_name
    value = '{"Type": "rewrite", "Middleware":{"Regexp": "^http://(.*)$", "Replacement": "https://$1", "Redirect": true}}'

    return insert(key, value, 'Added https redirect middleware : %s' % key)

def add_rate_limiting(backend_name):
    key = '/vulcand/frontends/%s/middlewares/rate' % backend_name
    value = '{"Type": "ratelimit", "Middleware":{"Requests": 100, "PeriodSeconds": 1, "Burst": 3, "Variable": "client.ip"}}'

    return insert(key, value, 'Added rate limiting middleware : %s' % key)

# -------------------------------------------------------------------------

def remove_frontend(backend_name):
    key = '/vulcand/frontends/%s/frontend' % backend_name
    remove(key, 'remove frontend : %s' % backend_name)

def add_container(container):
    server_name = container.name
    backend_name = server_name.split('-')[0]

    ROUTE = get_envvar(container, 'ROUTE')
    PORT = get_envvar(container, 'PORT')

    if not ROUTE:
        logging.warning('No route found for container: ' + server_name)
        return

    if not PORT:
        logging.warning('No port could be found for this container' + container_name)

    create_backend(backend_name)
    create_server(container, server_name, backend_name, ROUTE, PORT)

    create_frontend(backend_name, ROUTE)

    add_rate_limiting(backend_name)
    add_https_redirect(backend_name)

def remove_container(container):
    server_name = container.name
    backend_name = server_name.split('-')[0]

    key = '/vulcand/backends/%s/servers/%s' % (backend_name, server_name)

    # Same thing here, we only remove the server, not the frontend or the backend
    remove(key, 'Removed server: %s' % key)

# -------------------------------------------------------------------------

def on_message(message):
    message = json.loads(message)

    if 'type' in message:
        if message['type'] == 'container':
            if 'action' in message:
                if message['action'] == 'update':
                    if message['state'] == 'Running':
                        logging.warning('Running')
                        container = get_container(message)
                        add_container(container)

                    elif message['state'] == 'Stopped': 
                        logging.warning('Stopped')
                        container = get_container(message)
                        remove_container(container)

                elif message['action'] == 'delete':
                      if message['state'] == 'Terminated':
                        logging.warning('Terminated')
                        container = get_container(message)
                        remove_container(container)

def on_error(error):
    logging.error(error)

def create_listener(name, protocol, address):
    key = '/vulcand/listeners/%s' % name
    value = '{"Protocol":"%s", "Address":{"Network":"tcp", "Address":"%s"}}' % (protocol, address)

    return insert(key, value, 'Added a listener: %s on %s' % (name, address))

event_manager.on_open(on_open)
event_manager.on_close(on_close)
event_manager.on_error(on_error)
event_manager.on_message(on_message)

# FIXME : needed?
create_listener('http', 'http', "0.0.0.0:80")
#create_listener('https', 'https', "0.0.0.0:443")
#create_listener('ws', 'ws', "0.0.0.0:8000") # FIXME websockets, wss

event_manager.run_forever()
