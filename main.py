from google.appengine.dist import use_library
use_library('django', '1.2')

import os
import logging
import uuid
import time
from google.appengine.api import users, channel, urlfetch, memcache
from google.appengine.ext import webapp, db
from google.appengine.ext.webapp import util, template, RequestHandler

import simplejson

#GEO_URL = "http://geoip.pidgets.com?ip=%s&format=json"
# temporary proxy
GEO_URL = "http://abort.boom.net/~pfnguyen/geoip.cgi/%s"

class Message(db.Model):
    message = db.StringProperty()
    nick = db.StringProperty()
    ip = db.StringProperty()
    ts = db.DateTimeProperty(auto_now_add=True)
    geo = db.GeoPtProperty()

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
        ip = "129.42.38.1" # something in NY

    geo = urlfetch.fetch(GEO_URL % ip, None, "GET")
    geoinfo = simplejson.loads(geo.content)
    if not geoinfo:
        geoinfo = { 'latitude': 25.443275, 'longitude': -70.576172 }
    memcache.set("geo" + ip, simplejson.dumps(geoinfo))

    return (geoinfo['latitude'],geoinfo['longitude'])

def messagedict(m):
    d = {}
    d['lat'] = m.geo.lat
    d['lon'] = m.geo.lon
    d['msg'] = m.message
    d['nick'] = m.nick
    d['ts'] = time.mktime(m.ts.timetuple()) * 1000
    return d

def get_channels():
    c = memcache.get("channels")
    if not c:
        channels = {}
    else:
        channels = simplejson.loads(c)
    return channels

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
    
class PingHandler(Handler):
    def post(self):
        userid = self.request.body.strip()
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
        m = simplejson.loads(self.request.body)
        message = Message()
        message.message = m['message']
        message.nick = m['nick']
        ip = self.request.environ['REMOTE_ADDR']
        message.ip = ip
        (lat, lon) = geolocate(ip)
        message.geo = db.GeoPt(lat, lon)
        message.put()
        channels = get_channels()
        for k in channels.keys():
            channel.send_message(k, simplejson.dumps(messagedict(message)))

class MainHandler(Handler):
    def get(self):
        (lat, lon) = geolocate(self.request.environ['REMOTE_ADDR'])[0:2]
        userid = str(uuid.uuid4())
        self.render("index.html", {
            'lat': lat, 'lon': lon,
            'channel': channel.create_channel(userid),
            'userid': userid,
            'users': len(get_channels())
        });

urls = [
    ('/',         MainHandler),
    ('/send',     SendHandler),
    ('/playback', PlaybackHandler),
    ('/ping',     PingHandler),
]

def main():
    application = webapp.WSGIApplication(urls, debug=True)
    util.run_wsgi_app(application)
if __name__ == '__main__':
    main()
