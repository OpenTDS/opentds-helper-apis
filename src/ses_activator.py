"""CloudFormation custom resource: make this stack's SES receipt rule set the
active one. Only one rule set can be active per account and region, so it
refuses to replace a different active set unless Force is true."""
import json
import urllib.request

import boto3

ses = boto3.client('ses')


def _send(event, context, status, data=None, reason=None, physical_id=None):
    """Signal CloudFormation. Hand-rolled rather than relying on the
    injected cfnresponse module so behaviour is explicit."""
    payload = json.dumps({
        'Status': status,
        'Reason': reason or 'see log stream ' + context.log_stream_name,
        'PhysicalResourceId': physical_id or context.log_stream_name,
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'NoEcho': False,
        'Data': data or {},
    }).encode('utf-8')
    req = urllib.request.Request(
        event['ResponseURL'], data=payload, method='PUT',
        headers={'content-type': '', 'content-length': str(len(payload))})
    urllib.request.urlopen(req)


def _active():
    try:
        meta = ses.describe_active_receipt_rule_set().get('Metadata') or {}
        return meta.get('Name')
    except Exception:
        return None


def handler(event, context):
    props = event.get('ResourceProperties') or {}
    rule_set = props.get('RuleSetName')
    force = str(props.get('Force', 'false')).lower() == 'true'
    physical = 'ses-active-rule-set/' + str(rule_set)

    try:
        if event['RequestType'] == 'Delete':
            # Never block a stack delete on this. Deactivate only if
            # the active set is still ours; swallow anything else.
            try:
                if _active() == rule_set:
                    ses.set_active_receipt_rule_set()
            except Exception as exc:
                print('deactivate failed, continuing: %s' % exc)
            _send(event, context, 'SUCCESS', physical_id=physical)
            return

        current = _active()
        if current and current != rule_set and not force:
            _send(event, context, 'FAILED', physical_id=physical, reason=(
                "Receipt rule set '%s' is already active in this account and "
                "region. Activating '%s' would replace it and silently stop "
                "whatever inbound mail depends on it. Redeploy with "
                "ForceActivateReceiptRuleSet=true to replace it deliberately, "
                "or ActivateReceiptRuleSet=false to leave it alone."
                % (current, rule_set)))
            return

        ses.set_active_receipt_rule_set(RuleSetName=rule_set)
        _send(event, context, 'SUCCESS', physical_id=physical, data={
            'ActiveRuleSet': rule_set,
            'PreviousActiveRuleSet': current or '(none)',
        })
    except Exception as exc:
        try:
            _send(event, context, 'FAILED', physical_id=physical, reason=str(exc))
        except Exception:
            print('could not signal CloudFormation: %s' % exc)
            raise
