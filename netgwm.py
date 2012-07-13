#!/usr/bin/python
# -*- coding: utf-8 -*-

import sys, os, stat
import yaml
import time
import socket
import optparse
import re

configfile  = '/etc/netgwm/netgwm.yml'
gwstorefile = '/var/run/netgwm/gwstore.yml'
modefile    = '/var/lib/netgwm/mode'

def main():
    parser = optparse.OptionParser(add_help_option = False)
    parser.add_option('-h', '--help', action = 'help')
    parser.add_option('-c', '--config', default = configfile)
    options, args = parser.parse_args()

    config = yaml.load(open(options.config, 'r'))
    if not os.path.exists('/var/run/netgwm/'): os.mkdir('/var/run/netgwm/')

    try:    gwstore = yaml.load(open(gwstorefile, 'r'))
    except: gwstore = {}

    gateways = []
    if 'gateways' in config and not config['gateways'] is None:
      for gw_identifier, gw_data in config['gateways'].iteritems():
          gateways.append(GatewayManager(gwstore, identifier=gw_identifier, **gw_data))

    currentgw = GatewayManager.get_current_gateway(gateways)

    try:
        if 'mode' in config: mode = config['mode']
        else:                mode = open(modefile, 'r').read().strip()
        if mode not in config['gateways']: raise Exception()
    except: mode = 'auto'

    if mode == 'auto':
        if currentgw is not None and currentgw.check(config['check_sites']):
            # если доступен интернет
            # ищем доступный роутер с приоритетом выше, чем у текущего
            candidates = [x for x in gateways if x.priority < currentgw.priority]
            for gw in sorted(candidates, key = lambda x: x.priority):
                if gw.check(config['check_sites']) and gw.wakeuptime < (time.time() - config['min_uptime']):
                    # роутер работает и работает без сбоев достаточно долго
                    gw.setdefault()
                    post_replace_trigger(newgw=gw, oldgw=currentgw)
                    break
                else: continue
        else:
            # Срочно переключаемся на самый приоритетный доступный шлюз
            for gw in sorted(gateways, key = lambda x: x.priority):
                if gw == currentgw: continue # и так понятно, что текущий роутер не работает
                if gw.check(config['check_sites']):
                    gw.setdefault()
                    post_replace_trigger(newgw=gw, oldgw=currentgw)
                    break
                else: continue
            # ни один роутер не работает
    else:
        fixedgw = [x for x in gateways if x.identifier == mode].pop()
        if currentgw is None or currentgw != fixedgw:
            fixedgw.setdefault()
            post_replace_trigger(newgw=fixedgw, oldgw=currentgw)

    if 'check_all_gateways' in config and config['check_all_gateways'] is True:
        for gw in [x for x in gateways if not x.is_checked]: gw.check(config['check_sites'])

    GatewayManager.store_gateways(gateways)


def post_replace_trigger(newgw, oldgw):
    # post-replace.d
    args = []
    args.append(newgw.identifier)
    args.append(newgw.ip  if hasattr(newgw, 'ip')  else 'NaN')
    args.append(newgw.dev if hasattr(newgw, 'dev') else 'NaN')
    args.append(oldgw.identifier if not oldgw is None else 'Nan')
    args.append(oldgw.ip         if not oldgw is None and hasattr(oldgw, 'ip')  else 'NaN')
    args.append(oldgw.dev        if not oldgw is None and hasattr(oldgw, 'dev') else 'NaN')
    for filename in sorted(os.listdir('/etc/netgwm/post-replace.d/')):
        execpath = '/etc/netgwm/post-replace.d/'+filename
        if os.path.isfile(execpath) and (os.stat(execpath).st_mode & stat.S_IXUSR):
            os.system(execpath+' '+' '.join(args))


class GatewayManager:
    def __init__(self, gwstore, **kwargs):
        self.priority   = kwargs['priority']
        self.identifier = kwargs['identifier']
        self.is_checked = False
        if 'ip'  in kwargs and kwargs['ip']  is not None: self.ip  = kwargs['ip']
        if 'dev' in kwargs and kwargs['dev'] is not None: self.dev = kwargs['dev']

        if self.identifier in gwstore: self.wakeuptime = gwstore[self.identifier]['wakeuptime']
        else: self.wakeuptime = 0 # считаем, что при первом появлении роутера в системе, его аптайм -- много лет.

    def __eq__(self, other):
        if other is None: return False
        else:             return self.identifier == other.identifier

    def check(self, check_sites):
        # check gw status
        print 'checking ' + self.identifier
        ipresult = not os.system('/sbin/ip route replace default %s table netgwm_check' % self.generate_route())

        if ipresult is True:
            for site in check_sites:
                site_ip = socket.gethostbyname(site)
    
                os.system('/sbin/ip rule add iif lo to %s lookup netgwm_check' % site_ip)
    
                p       = os.popen('ping -q -n -W 1 -c 2 %s 2> /dev/null' % site_ip)
                pingout = p.read()
                status  = not p.close()
    
                os.system('/sbin/ip rule del iif lo to %s lookup netgwm_check' % site_ip)
    
                if status is True:
                    # ping success
                    rtt  = re.search('\d+\.\d+/(\d+\.\d+)/\d+\.\d+/\d+\.\d+', pingout).group(1)
                    info = 'up:'+site+':'+rtt
                    break
                else:
                    # ping fail
                    info = 'down'
            os.system('/sbin/ip route del default %s table netgwm_check' % self.generate_route())
        else:
            status = False
            info   = 'down'
        
        try: 
            with open('/var/run/netgwm/'+self.identifier, 'w') as f: f.write(info)
        except: pass

        if self.wakeuptime is None and status is True: self.wakeuptime = time.time() # Если не установлено время подъема и сервак пинганулся -- устанавливае
        elif status is False:                          self.wakeuptime = None        # Если не пинганулся -- затираем

        self.is_checked = True

        return status

    def setdefault(self):
        # replace
        print '/sbin/ip route replace default ' + self.generate_route()
        os.system('/sbin/ip route replace default ' + self.generate_route())

    def generate_route(self):
        res = []
        if hasattr(self, 'ip'):  res.append('via ' + self.ip)
        if hasattr(self, 'dev'): res.append('dev ' + self.dev)
        return ' '.join(res)

    @staticmethod
    def get_current_gateway(gateways):
        currentgw_ip  = os.popen("/sbin/ip route | grep 'default via' | sed -r 's/default via (([0-9]+\.){3}[0-9]+) dev .+/\\1/g'").read().strip()
        currentgw_dev = os.popen("/sbin/ip route | grep 'default dev' | sed -r 's/default dev ([a-z0-9]+)(\s+.*)?/\\1/g'").read().strip()

        if currentgw_ip == '' and currentgw_dev == '': 
            return None
        elif currentgw_ip != '':
            for g in [x for x in gateways if hasattr(x, 'ip')]:
                if g.ip == currentgw_ip: return g 
        elif currentgw_dev != '':
            for g in [x for x in gateways if hasattr(x, 'dev')]:
                if g.dev == currentgw_dev: return g 
        else: raise Exception('current gw is not listed in config.')
        
    @staticmethod
    def store_gateways(gateways):
        gwstore = {}
        for gw in gateways: gwstore[gw.identifier] = {'wakeuptime': gw.wakeuptime}
        open(gwstorefile, 'w').write(yaml.dump(gwstore))

 
if __name__ == '__main__':
    main()
