# Security

Use **Security → Report a vulnerability** on this repository when the form is
available. If you cannot access it, open an issue asking for a private reporting
channel. Keep vulnerability details out of that issue.

In the private report, include the commit you used, what happened, the impact,
and steps to reproduce it. Remove credentials, physical device identities,
private payloads and customer data from examples, logs and captures. Ordinary
bugs can be reported in Issues.

BLE configuration can select external capture and firmware tools. Review the
configuration and executable paths before using them. AutoSpan's hardware gates
control device acquisition; they do not sandbox Python plugins or external tools.
