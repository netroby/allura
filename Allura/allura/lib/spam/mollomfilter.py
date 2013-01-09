import logging

from pylons import request
from pylons import tmpl_context as c

from allura.lib import helpers as h
from allura.lib.spam import SpamFilter

import Mollom


log = logging.getLogger(__name__)


class MollomSpamFilter(SpamFilter):
    """Spam checking implementation via Mollom service.

    To enable Mollom spam filtering in your Allura instance, first
    enable the entry point in setup.py::

        [allura.spam]
        mollom = allura.lib.spam.mollomfilter:MollomSpamFilter

    Then include the following parameters in your .ini file::

        spam.method = mollom
        spam.public_key = <your Mollom public key here>
        spam.private_key = <your Mollom private key here>
    """
    def __init__(self, config):
        self.service = Mollom.MollomAPI(
            publicKey=config.get('spam.public_key'),
            privateKey=config.get('spam.private_key'))
        if not self.service.verifyKey():
            raise Mollom.MollomFault('Your MOLLOM credentials are invalid.')

    def check(self, text, artifact=None, user=None, content_type='comment', **kw):
        """Basic content spam check via Mollom. For more options
        see http://mollom.com/api#api-content
        """
        log_msg = text
        kw['postBody'] = text
        if artifact:
            # Should be able to send url, but can't right now due to a bug in
            # the PyMollom lib
            # kw['url'] = artifact.url()
            log_msg = artifact.url()
        user = user or c.user
        if user:
            kw['authorName'] = user.display_name or user.username
            kw['authorMail'] = user.email_addresses[0] if user.email_addresses else ''
        user_ip = request.headers.get('X_FORWARDED_FOR', request.remote_addr)
        kw['authorIP'] = user_ip.split(',')[0].strip()
        # kw will be urlencoded, need to utf8-encode
        for k, v in kw.items():
            kw[k] = h.really_unicode(v).encode('utf8')
        cc = self.service.checkContent(**kw)
        res = cc['spam'] == 2
        log.info("spam=%s (mollom): %s" % (str(res), log_msg))
        return res

