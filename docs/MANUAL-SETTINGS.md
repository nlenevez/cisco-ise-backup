# Settings rebuilt by hand

Phase 7 of [RESTORE-SOP.md](RESTORE-SOP.md): settings with no API that
`ise-config` can use. Fill in the Value column while the cluster is
healthy; when you need this list, the old cluster may not start.

| # | Setting | GUI path | Value | Source in git |
|--:|---------|----------|-------|---------------|
| 1 | Admin users (passwords are never exported) | Administration > System > Admin Access > Administrators > Admin Users | | `config/rbac/admin-users.json` |
| 2 | Custom admin groups, menu access, data access, RBAC policies | Administration > System > Admin Access > Authorization | | `config/rbac/*.json` |
| 3 | Admin access: session timeout, password policy, IP access, authentication method | Administration > System > Admin Access > Settings / Authentication | | |
| 4 | System certificates (Admin, EAP, Portal, pxGrid, SAML) | Administration > System > Certificates > System Certificates | | `config/system/system-certificates/` (usage only) |
| 5 | Repositories (passwords not exported) | Administration > System > Maintenance > Repository | | `config/system/repositories.json` |
| 6 | Scheduled backups | Administration > System > Backup & Restore | | |
| 7 | Remote logging targets and categories | Administration > System > Logging | | |
| 8 | SMTP server | Administration > System > Settings > SMTP Server | | |
| 9 | Proxy | Administration > System > Settings > Proxy | | `config/system/proxy.json` |
| 10 | Security settings: TLS versions, ciphers | Administration > System > Settings > Security Settings | | |
| 11 | Protocol settings: EAP, RADIUS and TACACS+ global settings | Administration > System > Settings > Protocols | | |
| 12 | Alarm settings and recipients | Administration > System > Settings > Alarm Settings | | |
| 13 | Node services and profiler probes | Administration > System > Deployment > *node* | | `config/system/deployment-nodes.json` |
| 14 | Custom profiler policies | Work Centers > Profiler > Profiling Policies | | not exported |
| 15 | Network device profiles | Administration > Network Resources > Network Device Profiles | | |
| 16 | Posture | Work Centers > Posture | | export API needs 3.5 Patch 4 |
| 17 | Client provisioning | Work Centers > Posture > Client Provisioning | | |
| 18 | SAML, RSA, ODBC and other external identity sources | Administration > Identity Management > External Identity Sources | | |
| 19 | pxGrid settings and clients | Administration > pxGrid Services | | |
| 20 | Portal images and themes | Work Centers > Guest Access > Portals & Components | | `config/ers/portaltheme/` |
| 21 | Licensing | Administration > System > Licensing | | |
| 22 | Duo connection secret keys | Administration > Identity Management > External Identity Sources > Duo | Duo Admin Panel | `config/system/duo-mfa/` |
| 23 | Duo identity sync | same page, Identity Sync | | `config/system/duo-identitysync/` |
| 24 | Internal user passwords | distribute `~/ise-generated-passwords.tsv` from phase 4 | | |
