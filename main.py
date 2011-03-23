from google.appengine.dist import use_library
use_library('django', '1.2')

import os
import logging
import uuid
import time
from google.appengine.api.channel import InvalidChannelClientIdError
from google.appengine.api import users, channel, urlfetch, memcache
from google.appengine.ext import webapp, db
from google.appengine.ext.webapp import util, template, RequestHandler
import simplejson

GEO_URL = "http://abort.boom.net/~pfnguyen/geoip.cgi/%s"
BERMUDA_TRIANGLE = { 'latitude': 25.443275, 'longitude': -70.576172 }

class Message(db.Model):
    user    = db.StringProperty()
    message = db.StringProperty()
    nick    = db.StringProperty()
    ip      = db.StringProperty()
    ts      = db.DateTimeProperty(auto_now_add=True)
    geo     = db.GeoPtProperty()

class Handler(RequestHandler):
    def user(self):
        return users.get_current_user()

    def render(self, tmpl, params={}):
        self.response.out.write(self.process(tmpl, params))

    def process(self, tmpl, params={}):
        user = self.user()

        p = {
            'logged_in':    user != None,
            'current_user': user,
            'login_url':    users.create_login_url(self.request.uri),
            'logout_url':   users.create_logout_url('/'),
            'request_uri':  self.request.path,
        }
        p.update(params)

        path = os.path.join(os.path.dirname(__file__), "templates", tmpl)
        return template.render(path, p)

    def respond(self, body, status=200):
        if status != 200:
            self.error(status)
        self.response.out.write(body)

def geolocate(ip):
    g = memcache.get("geo" + ip)
    if g:
        geoinfo = simplejson.loads(g)
        return (geoinfo['latitude'], geoinfo['longitude'])

    if ip.find("192.168") == 0:
        ip = "129.42.38.1" # something in NY, testing

    geo = urlfetch.fetch(GEO_URL % ip, None, "GET")
    geoinfo = simplejson.loads(geo.content)
    if not geoinfo:
        geoinfo = BERMUDA_TRIANGLE
    memcache.set("geo" + ip, simplejson.dumps(geoinfo))

    return (geoinfo['latitude'],geoinfo['longitude'])

def messagedict(m):
    d = {}
    d['user'] = m.user
    d['lat']  = m.geo.lat
    d['lon']  = m.geo.lon
    d['msg']  = m.message
    d['nick'] = m.nick
    d['ts']   = time.mktime(m.ts.timetuple()) * 1000
    return d

def get_channels():
    c = memcache.get("channels")
    if not c:
        channels = {}
    else:
        channels = simplejson.loads(c)
    return channels

# TODO check if size > 1mb, if so, purge old entries until <1mb
def purge_channels(userid=None):
    c = memcache.get("channels")
    now = time.time()
    if not c:
        channels = {}
    else:
        channels = simplejson.loads(c)
    if userid:
        channels[userid] = str(now)
    for k in channels.keys():
        if (now - float(channels[k])) > 60:
            del channels[k]
    memcache.set("channels", simplejson.dumps(channels))
    return len(channels)
    
# set geoip if we fail to find it using maxmind
class GeoIPHandler(Handler):
    def post(self):
        ip = self.request.environ['REMOTE_ADDR']
        g = None
        geoinfo = None
        if self.request.path != '/geoip-html5':
            g = memcache.get("geo%s" % ip)
        if not g:
            geoinfo = simplejson.loads(self.request.body)
        else:
            geo = simplejson.loads(g);
            if geo == BERMUDA_TRIANGLE:
                geoinfo = simplejson.loads(self.request.body)
        if geoinfo:
            geo = {
                'latitude':  geoinfo['latitude'],
                'longitude': geoinfo['longitude']
            }
            memcache.set("geo%s" % ip, simplejson.dumps(geo))
            self.respond("false")
        else:
            self.respond("true")

# this can result in a strange side-effect of geo changing if it expires
# from memcached and the client reports a different lat/lng
class PingHandler(Handler):
    def post(self):
        body   = simplejson.loads(self.request.body)
        userid = body['id']
        lat    = body['lat']
        lon    = body['lng']
        ip     = self.request.environ['REMOTE_ADDR']
        g      = memcache.get("geo%s" % ip)
        if not g:
            geo = {
                'latitude':  lat,
                'longitude': lon
            }
            memcache.set("geo%s" % ip, simplejson.dumps(geo))

        count = purge_channels(userid)
        self.respond(str(count))

class PlaybackHandler(Handler):
    def get(self):
        messages = Message.all().order("-ts").fetch(100)
        messages.reverse()
        m = []
        for msg in messages:
            m.append(messagedict(msg))
        self.respond(simplejson.dumps(m))

class SendHandler(Handler):
    def post(self):
        m               = simplejson.loads(self.request.body)
        ip              = self.request.environ['REMOTE_ADDR']
        (lat, lon)      = geolocate(ip)
        message         = Message()
        message.user    = m['user']
        message.message = m['message']
        message.nick    = m['nick']
        message.ip      = ip
        message.geo     = db.GeoPt(lat, lon)
        message.put()

        channels = get_channels()
        for k in channels.keys():
            try:
                channel.send_message(k, simplejson.dumps(messagedict(message)))
            except InvalidChannelClientIdError, e:
                logging.error("Unable to send to channel: %s => %s" % (k, e))

class PurgeHandler(Handler):
    def get(self):
        old = db.GqlQuery("SELECT __key__ FROM Message " +
                "ORDER BY ts DESC OFFSET 100")
        db.delete(old)

class MainHandler(Handler):
    def get(self):
        (lat, lon) = geolocate(self.request.environ['REMOTE_ADDR'])[0:2]
        if self.request.cookies.has_key('chatuser'):
            userid = self.request.cookies['chatuser']
        else:
            userid = str(uuid.uuid4())
        self.render("index.html", {
            'lat':     lat,
            'lon':     lon,
            'channel': channel.create_channel(userid),
            'userid':  userid,
            'users':   len(get_channels())
        });

class ChannelTokenHandler(Handler):
    def post(self):
        if self.request.cookies.has_key('chatuser'):
            userid = self.request.cookies['chatuser']
        else:
            self.respond("Error", 403)
            return
        self.respond(channel.create_channel(userid))

urls = [
    ('/',              MainHandler),
    ('/send',          SendHandler),
    ('/playback',      PlaybackHandler),
    ('/ping',          PingHandler),
    ('/purge',         PurgeHandler),
    ('/geoip',         GeoIPHandler),
    ('/geoip-html5',   GeoIPHandler),
    ('/channel-token', ChannelTokenHandler),
]

def main():
    application = webapp.WSGIApplication(urls, debug=True)
    util.run_wsgi_app(application)
if __name__ == '__main__':
    main()
