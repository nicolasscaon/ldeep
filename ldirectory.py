
from struct import unpack
from socket import inet_ntoa

from ldap3 import ALL_ATTRIBUTES, ALL_OPERATIONAL_ATTRIBUTES, Server, Connection, SASL, KERBEROS, NTLM, SUBTREE, ALL
from ldap3.protocol.formatters.formatters import format_sid, format_uuid, format_ad_timestamp
from ldap3.protocol.formatters.validators import validate_sid, validate_guid
from ldap3.core.exceptions import LDAPNoSuchObjectResult, LDAPOperationResult
from ldap3.extend.microsoft.unlockAccount import ad_unlock_account

from ldap_utils import DNS_TYPES, USER_ACCOUNT_CONTROL, WELL_KNOWN_SIDs


PAGE_SIZE = 1000
ALL = [ALL_ATTRIBUTES, ALL_OPERATIONAL_ATTRIBUTES]


# define an ldap3-compliant formatters
def format_userAccountControl(raw_value):
	try:
		val = int(raw_value)
		result = []
		for k, v in USER_ACCOUNT_CONTROL.items():
			if v & val:
				result.append(k)
		return " | ".join(result)
	except (TypeError, ValueError):  # expected exceptions↲
		pass
	except Exception:  # any other exception should be investigated, anyway the formatter return the raw_value
		pass
	return raw_value


def format_dnsrecord(raw_value):
	databytes = raw_value[0:4]
	datalen, datatype = unpack("HH", databytes)
	data = raw_value[24:24 + datalen]
	for recordname, recordvalue in DNS_TYPES.items():
		if recordvalue == datatype:
			if recordname == "A":
				target = inet_ntoa(data)
			else:
				# how, ugly
				data = data.decode('unicode-escape')
				target = ''.join([c for c in data if ord(c) > 31 or ord(c) == 9])
			return "%s %s" % (recordname, target)


import ldap3
ldap3.protocol.formatters.standard.standard_formatter["1.2.840.113556.1.4.8"] = (format_userAccountControl, None)
ldap3.protocol.formatters.standard.standard_formatter["1.2.840.113556.1.4.382"] = (format_dnsrecord, None)


class ActiveDirectoryView(object):
	"""
	Manage a LDAP connection to a LDAP Active Directory.
	"""

	class ActiveDirectoryLdapException(Exception):
		pass

	class ActiveDirectoryLdapInvalidSID(Exception):
		pass

	class ActiveDirectoryLdapInvalidGUID(Exception):
		pass

	def __init__(self, server, fqdn, base="", username="", password="", method="NTLM"):
		"""
		ActiveDirectoryView constructor.
		Initialize the connection with the LDAP server.

		Two authentication modes:
			* Kerberos (ldap3 will automatically retrieve the $KRB5CCNAME env variable)
			* NTLM (username + password or NTLM hash)

		@server: Server to connect and perform LDAP query to.
		@fqdn: Fully qualified domain name of the Active Directory domain.
		@base: Base for the LDAP queries.
		@username: Username to use for the authentication (for NTLM authentication)
		@password: Username to use for the authentication (for NTLM authentication)
		@method: Either to use NTLM or Kerberos authentication.

		@throw ActiveDirectoryLdapException when the connection or the bind does not work.
		"""
		self.username = username
		self.password = password
		self.server = server
		self.fqdn = fqdn
		self.hostnames = []

		if method == "Kerberos":
			server = Server(self.server)
			self.ldap = Connection(server, authentication=SASL, sasl_mechanism=KERBEROS)
		elif method == "NTLM":
			server = Server(self.server)
			self.ldap = Connection(
				server,
				user="{domain}\\{username}".format(domain=fqdn, username=username),
				password=password,
				authentication=NTLM, check_names=True
			)

		if not self.ldap.bind():
			raise ActiveDirectoryLdapException("Unable to bind with provided information")

		self.base_dn = base or ','.join(["dc={}".format(d) for d in fqdn.split(".")])
		self.search_scope = SUBTREE

	def query(self, ldapfilter, attributes=[ALL_ATTRIBUTES, ALL_OPERATIONAL_ATTRIBUTES], base=None, scope=None):
		"""
		Perform a query to the LDAP server and return the results.

		@ldapfiler: The LDAP filter to query (see RFC 2254).
		@attributes: List of attributes to retrieved with the query.
		@base: Base to use during the request.
		@scope: Scope to use during the request.

		@return a list of records.
		"""
		result_set = []
		try:
			entry_generator = self.ldap.extend.standard.paged_search(
				search_base=base or self.base_dn,
				search_filter=ldapfilter,
				search_scope=scope or self.search_scope,
				attributes=attributes,
				paged_size=PAGE_SIZE,
				generator=True
			)

			for entry in entry_generator:
				if "dn" in entry:
					d = entry["attributes"]
					d["dn"] = entry["dn"]
					result_set.append(d)

		except LDAPOperationResult as e:
			raise self.ActiveDirectoryLdapException(e)

		return result_set

	def resolve_sid(self, sid):
		"""
		Two cases:
			* the SID is a WELL KNOWN SID and a local SID, the name of the corresponding account is returned;
			* else, the SID is search through the LDAP and the corresponding record is returned.

		@sid: the sid to search for.

		@throw ActiveDirectoryLdapInvalidSID if the SID is not a valid SID.
		@return the record corresponding to the SID queried.
		"""
		if sid in WELL_KNOWN_SIDs:
			return WELL_KNOWN_SIDs[sid]
		elif validate_sid(sid):
			results = self.query("(&(ObjectSid={sid}))".format(sid=sid))
			if results:
				return results
		raise self.ActiveDirectoryLdapInvalidSID("SID: {sid}".format(sid=sid))

	def resolve_guid(self, guid):
		"""
		Return the LDAP record with the provided GUID.

		@guid: the guid to search for.

		@throw ActiveDirectoryLdapInvalidGUID if the GUID is not a valid GUID.
		@return the record corresponding to the guid queried.
		"""
		if validate_guid(guid):
			results = self.query("(&(ObjectGUID={guid}))".format(guid=guid))
			# Normally only one result should have been retrieved:
			if results:
				return results
		raise self.ActiveDirectoryLdapInvalidGUID("GUID: {guid}".format(guid=guid))

	def unlock(self, username):
		"""
		Unlock an account.

		@username: the username associated to the account to unlock.

		@throw ActiveDirectoryLdapException if the account does not exist or the query returns more than one result.
		@return True if the account was successfully unlock or False otherwise.
		"""
		results = self.query(USER_DN_FILTER.format(username=username))
		if len(results) != 1:
			raise ActiveDirectoryLdapException("Zero or non uniq result")
		else:
			user = results[0]
			info("Found user %s at DN %s" % (username, user["dn"]))
			unlock = ad_unlock_account(self.ldap, user["dn"])
			# goddamn, return value is either True or str...
			if isinstance(unlock, bool):
				return True
			else:
				return False