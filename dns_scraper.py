#!/usr/bin/env python


#   This file is part of DNS Scraper
#
#   Copyright (C) 2012 Ondrej Mikle, CZ.NIC Labs
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, version 3 of the License.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
The unbound-1.4.16 patch in "patches" directory is required for this to
work. See README.md.
"""

import time
import sys
import threading
import Queue
import logging
import struct

from datetime import datetime
from binascii import hexlify
from ConfigParser import SafeConfigParser

import ldns

from db import DbPool
from unbound import ub_ctx, ub_version, ub_strerror, ub_ctx_config, \
	RR_CLASS_IN, RR_TYPE_DNSKEY, RR_TYPE_A, \
	RR_TYPE_AAAA, RR_TYPE_SSHFP, RR_TYPE_MX, RR_TYPE_DS, RR_TYPE_NSEC, \
	RR_TYPE_NSEC3, RR_TYPE_NSEC3PARAMS, RR_TYPE_RRSIG, RR_TYPE_SOA, \
	RR_TYPE_NS, RR_TYPE_TXT, RCODE_SERVFAIL

RR_TYPE_SPF = 99

class DnsError(RuntimeError):
	"""Exception for reporting internal unbound and ldns errors"""
	
	def __init__(self, reason):
		super(DnsError, self).__init__(reason)

class DnsConfigOptions(object):
	"""Container for parameters present in config file related to DNS
	resolution.
	"""
	
	def __init__(self, scraperConfig):
		"""Load options from configParser.
		@param scraperConfig: instance of RawConfigParser or subclass
		"""
		self.unboundConfig = None
		self.forwarder = None
		self.attempts = scraperConfig.getint("dns", "retries")
		
		if scraperConfig.has_option("dns", "unbound_config"):
			self.unboundConfig = scraperConfig.get("dns", "unboundConfig")
		if scraperConfig.has_option("dns", "forwarder"):
			self.forwarder = scraperConfig.get("dns", "forwarder")
			
			
		
def result2pkt(result):
	"""Extract ldns packet from ub_result.
	
	@raises DnsError: on malformed packet
	"""
	status, pkt = ldns.ldns_wire2pkt(result.packet)
	
	if status != 0:
		raise DnsError("Failed to parse DNS packet: %s" % ldns.ldns_get_errorstr_by_id(status))
	
	return pkt
	
def validationToDbEnum(result):
	"""Given ub_result, returns on of three strings usable for the "secure"
	field in DB (secure, insecure, bogus).
	"""
	if result.secure:
		return "secure"
	elif result.bogus:
		return "bogus"
	else:
		return "insecure"

def getLdnsBufferData(buf, l):
	"""Return data from ldns_buffer buf of size l as pythonic string."""
	buf.flip()
	s = ""
	for i in range(l):
		s += chr(buf.read_u8())
	return s
	
def getRdfData(rdf):
	"""Return RDF bytes as pythonic string from ldns_rdf."""
	l = rdf.size()
	buf = ldns.ldns_buffer(l)
	rdf.write_to_buffer_canonical(buf)
	
	return getLdnsBufferData(buf, l)
	
def getRrData(rr, section=ldns.LDNS_SECTION_ANSWER):
	"""Return RR bytes as pythonic string from ldns_rr."""
	l = rr.uncompressed_size()
	buf = ldns.ldns_buffer(l)
	rr.write_to_buffer_canonical(buf, section)
	
	return getLdnsBufferData(buf, l)

def rdfConvert(rdf, fmt, conv=lambda x: x):
	"""Unpack and convert data from ldns_rdf.
	
	@param rdf: ldns_rdf
	@param fmt: format for struct.unpack
	@param conv: conversion function to call on result of struct.unpack()
	"""
	return conv(struct.unpack(fmt, getRdfData(rdf))[0])
		

class DnsMetadata(object):
	"""Represents DNS(SEC) metadata in answer: RRSIGs, NSEC and NSEC3 RRs.
	These are some things we need to parse from answer packet by ldns.
	"""
	
	def __init__(self, pkt, rrType):
		"""Fills self with parsed data from DNS answer.
		
		@param pkt: ldns_pkt DNS answer packet
		@param rrType: RR type to use for RR selection
		"""
		self.pkt = pkt
		self.rrType = rrType
		
	def rrsigs(self, section=ldns.LDNS_SECTION_ANSWER):
		"""Return RRSIGs from selected section"""
		rrsigs = self.pkt.rr_list_by_type(RR_TYPE_RRSIG, section)
		return rrsigs and [rrsigs.rr(i) for i in range(rrsigs.rr_count())] or []
		
	def rrsigsStore(self, domain, conn, section=ldns.LDNS_SECTION_ANSWER):
		"""Store RRSIGs from from given section of this packet in DB
		with connection conn. Stores each RRSIG in separate transaction.
		"""
		rrsigs = self.rrsigs(section)
		cursor = conn.cursor()
		sql = """INSERT INTO rrsig_rr
			(domain, ttl, rr_type, algo, labels, orig_ttl,
			sig_expiration, sig_inception,
			keytag, signer, signature)
			VALUES (%s, %s, %s, %s, %s, %s,
				to_timestamp(%s), to_timestamp(%s),
				%s, %s, %s)
			"""
		
		for rr in rrsigs:
			try:
				ttl = rr.ttl()
				algo = rdfConvert(rr.rrsig_algorithm(), "B")
				labels = rdfConvert(rr.rrsig_labels(), "B")
				orig_ttl = rdfConvert(rr.rrsig_origttl(), "!I")
				sig_expiration = rdfConvert(rr.rrsig_expiration(), "!I")
				sig_inception = rdfConvert(rr.rrsig_inception(), "!I")
				keytag = rdfConvert(rr.rrsig_keytag(), "!H")
				signer = str(rr.rrsig_signame()).rstrip(".")
				signature = buffer(getRdfData(rr.rrsig_sig()))
				
				sql_data = (domain, ttl, self.rrType, algo, labels, orig_ttl, sig_expiration,
					    sig_inception, keytag, signer, signature)
				cursor.execute(sql, sql_data)
				
			except:
				logging.exception("Failed to store RRSIG %s" % rr)
			finally:
				conn.commit()
		
	def nsecs(self):
		"""Return NSEC records from additional section"""
		nsecs  = self.pkt.rr_list_by_type(RR_TYPE_NSEC,  ldns.LDNS_SECTION_ADDITIONAL)
		return nsecs  and [nsecs.rr(i)  for i in range(nsecs.rr_count()) ] or []
		
	def nsec3s(self):
		"""Return NSEC3 records from additional section"""
		nsec3s = self.pkt.rr_list_by_type(RR_TYPE_NSEC3, ldns.LDNS_SECTION_ADDITIONAL)
		return nsec3s and [nsec3s.rr(i) for i in range(nsec3s.rr_count())] or []
		

class RRTypeParser(object):
	"""Abstract class for parsing and storing of all RR types."""
	
	rrType = 0 #undefined RR type
	rrClass = RR_CLASS_IN
	
	def __init__(self, domain, resolver, opts):
		"""Create instance.
		@param domain: domain to scan for
		@param resolver: ub_ctx to use for resolving
		@param opts: instance of DnsConfigOptions
		"""
		self.domain = domain
		self.resolver = resolver
		self.opts = opts
	
	def fetch(self):
		"""Generic fetching of record that are not of special form like
		SRV and TLSA.
		
		@returns: result part from (status, result) tupe of ub_ctx.resolve() or None on permanent SERVFAIL
		@throws: DnsError if unbound reports error
		"""
		for i in range(self.opts.attempts):
			(status, result) = self.resolver.resolve(self.domain, self.rrType, self.rrClass)
			
			if status != 0:
				raise DnsError("Resolving %s for %s: %s" % \
					(self.__class__.__name__, self.domain, ub_strerror(status)))
			
			if result.rcode != RCODE_SERVFAIL:
				logging.debug("Domain %s type %s: havedata %s, rcode %s", \
					self.domain, self.__class__.__name__, result.havedata, result.rcode_str)
				return result
			
			logging.warn("Permanent SERVFAIL: domain %s type %s", \
				self.domain, self.__class__.__name__)
			return None
		
	def fetchAndStore(self, conn):
		"""Scan records for the domain and store results in DB.
		@param conn: database connection from DBPool.connection()
		@return: number of records of given RR type found
		"""
		raise NotImplementedError
	

class AParser(RRTypeParser):
	
	rrType = RR_TYPE_A
	
	def __init__(self, domain, resolver, opts):
		RRTypeParser.__init__(self, domain, resolver, opts)
	
	def fetchAndStore(self, conn):
		try:
			r = RRTypeParser.fetch(self)
			if not r:
				return 0
		except DnsError:
			logging.exception("Fetching of %s failed" % self.domain)
			return 0
		
		if r.havedata:
			cursor = conn.cursor()
			secure = validationToDbEnum(r)
			pkt = result2pkt(r)
			
			rrs = pkt.rr_list_by_type(self.rrType, ldns.LDNS_SECTION_ANSWER)
			
			sql = """INSERT INTO aa_rr (secure, domain, ttl, addr)
				VALUES (%s, %s, %s, %s)
				"""
			for i in range(rrs.rr_count()):
				try:
					rr = rrs.rr(i)
					addr = str(rr.a_address())
					ttl = rr.ttl()
					
					sql_data = (secure, self.domain, ttl, addr)
					cursor.execute(sql, sql_data)
				except:
					logging.exception("Failed to store %s %s" % (rr.get_type_str(), rr))
				finally:
					conn.commit()
				
			meta = DnsMetadata(pkt, self.rrType)
			meta.rrsigsStore(self.domain, conn)
			
			return rrs.rr_count()
		
		return 0

class AAAAParser(AParser):
	
	rrType = RR_TYPE_AAAA
	
	
class RSAKey(object):

	def __init__(self, exponent, modulus, digest_algo, key_purpose):
		self.exponent = exponent
		self.modulus = modulus
		self.digest_algo = digest_algo
		self.key_purpose = key_purpose


class DnskeyAlgo:
	
	algoMap = \
		{ 
		     1: "RSA/MD5",
		     5: "RSA/SHA-1",
		     7: "RSASHA1-NSEC3-SHA1",
		     8: "RSA/SHA-256",
		    10: "RSA/SHA-512",
		}
	
	rsaAlgoIds = algoMap.keys()

class DNSKEYParser(RRTypeParser):

	rrType = RR_TYPE_DNSKEY
	maxDbExp = 9223372036854775807 #maximum exponent that fits in dnskey_rr.rsa_exp field
	
	def __init__(self, domain, resolver, opts):
		RRTypeParser.__init__(self, domain, resolver, opts)
	
	def fetchAndStore(self, conn):
		try:
			result = RRTypeParser.fetch(self)
			if not result:
				return 0
		except DnsError:
			logging.exception("Fetching of %s failed" % self.domain)
			return 0
		
		secure = validationToDbEnum(result)
		if result.havedata:
			cursor = conn.cursor()
			pkt = result2pkt(result)
			rrs = pkt.rr_list_by_type(self.rrType, ldns.LDNS_SECTION_ANSWER)
			
			sql = """INSERT INTO dnskey_rr
					(secure, domain, ttl, flags, protocol, algo, rsa_exp, rsa_mod, other_key)
					VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)
				"""
			
			for i in range(rrs.rr_count()):
				try:
					rr = rrs.rr(i)
					ttl = rr.ttl()
					flags = rdfConvert(rr.rdf(0), "!H")
					proto = rdfConvert(rr.rdf(1), "B")
					algo = rdfConvert(rr.rdf(2), "B")
					pubkey = getRdfData(rr.rdf(3))
					
					exponent = None
					modulus = None
					other_key = None
					
					if algo in DnskeyAlgo.rsaAlgoIds: #we have RSA key
						
						#RFC 2537/3110 exponent length encoding
						exp_len0 = ord(pubkey[0])
						if exp_len0 > 0:
							exp_len = exp_len0
							exp_hdr_len = 1
						else:
							exp_len = ord(pubkey[1]) << 8 + ord(pubkey[2])
							exp_hdr_len = 3
		
						exponent = int(hexlify(pubkey[exp_hdr_len:exp_hdr_len + exp_len]), 16)
						if exponent > self.maxDbExp: #needs to fit into DB field
							other_key = buffer(pubkey)
							exponent = -1
						else:
							modulus  = buffer(pubkey[exp_hdr_len + exp_len:].lstrip('\0'))
						
					else: #not a RSA key
						other_key = buffer(pubkey)
					
					sql_data = (secure, self.domain, ttl, flags, proto, algo, exponent, modulus, other_key)
					cursor.execute(sql, sql_data)
				except:
					logging.exception("Failed to store DNSKEY RR %s", rr)
				finally:
					conn.commit()
			
			meta = DnsMetadata(pkt, self.rrType)
			meta.rrsigsStore(self.domain, conn)
			
			return rrs.rr_count()
			
		return 0
			
	

class StorageThread(threading.Thread):
	"""Thread taking sql/sql_data from queue and executing it for storage in DB"""

	def __init__(self, db, dbQueue):
		"""Create storage thread.
		
		@param db: database connection pool, instance of db.DbPool
		@param dbQueue: instance of Queue.Queue that stores (sql,
		sql_data) tuples to be executed
		"""
		self.db = db
		self.dbQueue = dbQueue
		
		threading.Thread.__init__(self)

	def run(self):
		conn = self.db.connection()
		while True:
			sqlTuple = self.dbQueue.get()
			
			try:
				cursor = conn.cursor()
				sql, sql_data = sqlTuple
				cursor.execute(sql, sql_data)
			except Exception:
				logging.exception("Failed to execute `%s` with `%s`",
					sql, sql_data)
			finally:
				conn.commit()
				
			self.dbQueue.task_done()

class DnsScanThread(threading.Thread):

	def __init__(self, task_queue, ta_file, rr_scanners, db, opts):
		"""Create scanning thread.
		
		@param task_queue: Queue.Queue containing domains to scan as strings
		@param ta_file: trust anchor file for libunbound
		@param rr_scanners: list of subclasses of RRTypeParser to use for scan
		@param db: database connection pool, instance of db.DbPool
		@param opts: instance of DnsConfigOptions
		"""
		self.task_queue = task_queue
		self.rr_scanners = rr_scanners
		self.db = db
		self.opts = opts
		
		threading.Thread.__init__(self)
		
		self.resolver = ub_ctx()
		if opts.forwarder:
			self.resolver.set_fwd(opts.forwarder)
		self.resolver.add_ta_file(ta_file) #read public keys for DNSSEC verification

	def run(self):
		conn = self.db.connection()
		while True:
			domain = self.task_queue.get()
			
			for parserClass in self.rr_scanners:
				try:
					parser = parserClass(domain, self.resolver, self.opts)
					parser.fetchAndStore(conn)
				except Exception:
					logging.exception("Failed to scan domain %s with %s",
						domain, parserClass.__name__)
				
			self.task_queue.task_done()


def convertLoglevel(levelString):
	"""Converts string 'debug', 'info', etc. into corresponding
	logging.XXX value which is returned.
	
	@raises ValueError if the level is undefined
	"""
	try:
		return getattr(logging, levelString.upper())
	except AttributeError:
		raise ValueError("No such loglevel - %s" % levelString)


if __name__ == '__main__':
	if len(sys.argv) != 5: 
		print >> sys.stderr, "ERROR: usage: <domain_file> <ta_file> <thread_count> <scraper_config>" 
		sys.exit(1)
		
	domain_file = file(sys.argv[1])
	ta_file = sys.argv[2]
	thread_count = int(sys.argv[3])
	scraperConfig = SafeConfigParser()
	scraperConfig.read(sys.argv[4])
	
	#DNS resolution options
	opts = DnsConfigOptions(scraperConfig)
	if opts.unboundConfig:
		ub_ctx_config(opts.unboundConfig)
	
	#one DB connection per thread required
	db = DbPool(scraperConfig, max_connections=thread_count)
	
	logfile = scraperConfig.get("log", "logfile")
	loglevel = convertLoglevel(scraperConfig.get("log", "loglevel"))
	if logfile == "-":
		logging.basicConfig(stream=sys.stderr, level=loglevel,
			format="%(asctime)s %(levelname)s %(message)s [%(pathname)s:%(lineno)d]")
	else:
		logging.basicConfig(filename=logfile, level=loglevel,
			format="%(asctime)s %(levelname)s %(message)s [%(pathname)s:%(lineno)d]")
	
	#logging.info("Unbound version: %s", ub_version())
	
	task_queue = Queue.Queue(5000)
	
	parsers = [AParser, AAAAParser, DNSKEYParser]
	
	for i in range(thread_count):
		t = DnsScanThread(task_queue, ta_file, parsers, db, opts)
		t.setDaemon(True)
		t.start()
	
	start_time = time.time()
	domain_count = 0
	
	for line in domain_file:
		domain = line.rstrip()
		task_queue.put(domain)
		domain_count += 1
		
	task_queue.join()
	
	logging.info("Fetch of dnskeys for %d domains took %.2f seconds", domain_count, time.time() - start_time)
