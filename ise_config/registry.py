"""ERS object types: what is exported, apply order, reference and secret fields.

ERS_TYPES is in apply order: each type follows the types it references.
References are exported as the referenced object's name and mapped back to
the target's ids on apply. An id that cannot be resolved on export is
written as "<missing:uuid>" and reported as an error.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Ref:
    path: str          # dotted path inside the object; "x[]" iterates a list
    target: str        # ERS resource the id points at
    mode: str = "id"   # "id": one id; "csv": comma-separated ids in a string


@dataclass(frozen=True)
class ErsType:
    res: str
    refs: tuple = ()
    secrets: tuple = ()        # dotted paths of secret fields
    key: tuple = ()            # fields forming the name when there is no "name"
    volatile: tuple = ()       # top-level fields dropped on export
    lookup_only: bool = False  # listed for name<->id maps, never written
    list_only: bool = False    # no GET by id: the list row is the record
    optional: bool = False     # a 404/405 on list is not an error
    defer_fields: tuple = ()   # stripped on the "objects" stage, set on "policy"
    parent: str = ""           # field naming a parent of the same type
    # ERS has no PUT for some types. update_op names a substitute operation
    # (PUT /<res>/<id>/<op>); without one, changes are reported, not applied.
    no_update: bool = False
    update_op: str = ""
    notes: str = field(default="", compare=False)


ERS_TYPES = (
    ErsType("networkdevicegroup"),
    ErsType("networkdevice", secrets=(
        "authenticationSettings.radiusSharedSecret",
        "authenticationSettings.secondRadiusSharedSecret",
        "authenticationSettings.keyEncryptionKey",
        "authenticationSettings.messageAuthenticatorCodeKey",
        "tacacsSettings.sharedSecret",
        "snmpsettings.roCommunity",
        "trustsecsettings.deviceAuthenticationSettings.sgaDevicePassword",
        "trustsecsettings.deviceAuthenticationSettings.restApiPassword",
        "trustsecsettings.deviceConfigurationDeployment.enableModePassword",
        "trustsecsettings.deviceConfigurationDeployment.execModePassword",
        "tacacsSettings.previousSharedSecret",
        "snmpsettings.authPassword",
        "snmpsettings.privacyPassword",
    )),
    ErsType("identitygroup"),
    ErsType("endpointgroup", refs=(Ref("parentId", "endpointgroup"),),
            parent="parentId"),
    # Read-only in ERS (custom profiler policies are OpenAPI only).
    ErsType("profilerprofile", lookup_only=True),
    ErsType("internaluser",
            refs=(Ref("identityGroups", "identitygroup", "csv"),),
            secrets=("password", "enablePassword")),
    ErsType("sgt", volatile=("generationId",)),
    ErsType("sgacl", volatile=("generationId",)),
    ErsType("sgtvnvlan", optional=True),
    ErsType("filterpolicy", optional=True),
    # Only the default TrustSec matrix is handled: matrixId is a per-install
    # UUID with no ERS listing to map it.
    ErsType("egressmatrixcell", volatile=("matrixId",),
            refs=(Ref("sourceSgtId", "sgt"), Ref("destinationSgtId", "sgt"),
                  Ref("sgacls[]", "sgacl")),
            key=("sourceSgtId", "destinationSgtId")),
    ErsType("downloadableacl"),
    ErsType("authorizationprofile"),
    ErsType("certificateprofile", optional=True),
    # Groups can only be set once the join point has joined the domain.
    # No PUT on join points; addGroups takes the whole object instead.
    ErsType("activedirectory", defer_fields=("adgroups",), no_update=True,
            update_op="addGroups"),
    ErsType("ldap", optional=True, secrets=(
        "connectionSettings.primaryServer.adminPassword",
        "connectionSettings.secondaryServer.adminPassword")),
    ErsType("restidstore", optional=True),
    ErsType("idstoresequence"),
    ErsType("externalradiusserver", secrets=(
        "sharedSecret", "encryptionKey", "authenticatorKey")),
    ErsType("radiusserversequence"),
    ErsType("allowedprotocols"),
    ErsType("tacacscommandsets", optional=True),
    ErsType("tacacsprofile", optional=True),
    ErsType("tacacsexternalservers", optional=True, secrets=("sharedSecret",)),
    ErsType("tacacsserversequence", optional=True),
    ErsType("sgmappinggroup", optional=True, refs=(Ref("sgt", "sgt"),)),
    ErsType("sgmapping", optional=True,
            refs=(Ref("sgt", "sgt"), Ref("mappingGroup", "sgmappinggroup"))),
    ErsType("sxpvpns", optional=True, key=("sxpVpnName",), no_update=True),
    ErsType("sxpconnections", optional=True, key=("ipAddress", "sxpVpn")),
    ErsType("sxplocalbindings", optional=True, refs=(Ref("sgt", "sgt"),),
            key=("ipAddressOrHost", "sxpVpn")),
    ErsType("ancpolicy", optional=True),
    ErsType("guestlocation", optional=True, list_only=True),
    ErsType("guestssid", optional=True),
    ErsType("smsprovider", optional=True, list_only=True),
    ErsType("guestsmtpnotificationsettings", optional=True),
    ErsType("guesttype", optional=True),
    ErsType("sponsorgroup", optional=True),
    ErsType("portaltheme", optional=True),
    ErsType("portalglobalsetting", optional=True),
    ErsType("certificatetemplate", optional=True),
    ErsType("nspprofile", optional=True),
    ErsType("sponsoredguestportal", optional=True, volatile=("portalTestUrl",)),
    ErsType("selfregportal", optional=True, volatile=("portalTestUrl",)),
    ErsType("hotspotportal", optional=True, volatile=("portalTestUrl",)),
    ErsType("sponsorportal", optional=True, volatile=("portalTestUrl",)),
    ErsType("mydeviceportal", optional=True, volatile=("portalTestUrl",)),
    ErsType("byodportal", optional=True, volatile=("portalTestUrl",)),
    # Last; only statically assigned endpoints are configuration.
    ErsType("endpoint", refs=(Ref("groupId", "endpointgroup"),
                              Ref("profileId", "profilerprofile"))),
)

BY_RES = {t.res: t for t in ERS_TYPES}

# Policy services and the OpenAPI collections under each.
POLICY_SERVICES = ("network-access", "device-admin")
# Rule collections per policy set, in apply order. "mfa" (ISE 3.3+) and
# "exception" may answer 404.
RULE_KINDS = ("authentication", "mfa", "authorization", "exception")
OPTIONAL_KINDS = ("mfa", "exception")
