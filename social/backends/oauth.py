import six

from requests import HTTPError
from requests_oauthlib import OAuth1
from oauthlib.oauth1 import SIGNATURE_TYPE_AUTH_HEADER

from social.p3 import urlencode, unquote
from social.utils import url_add_parameters, parse_qs
from social.exceptions import AuthFailed, AuthCanceled, AuthUnknownError, \
                              AuthMissingParameter, AuthStateMissing, \
                              AuthStateForbidden, AuthTokenError
from social.backends.base import BaseAuth


class OAuthAuth(BaseAuth):
    """OAuth authentication backend base class.

    EXTRA_DATA defines a set of name that will be stored in
               extra_data field. It must be a list of tuples with
               name and alias.

    Also settings will be inspected to get more values names that should be
    stored on extra_data field. Setting name is created from current backend
    name (all uppercase) plus _EXTRA_DATA.

    access_token is always stored.
    """
    SCOPE_PARAMETER_NAME = 'scope'
    DEFAULT_SCOPE = None
    SCOPE_SEPARATOR = ' '
    EXTRA_DATA = None
    ID_KEY = 'id'
    ACCESS_TOKEN_METHOD = 'GET'

    def extra_data(self, user, uid, response, details=None):
        """Return access_token and extra defined names to store in
        extra_data field"""
        data = {'access_token': response.get('access_token', '')}
        names = (self.EXTRA_DATA or []) + \
                self.setting('EXTRA_DATA', [])
        for entry in names:
            if not isinstance(entry, (list, tuple)):
                entry = (entry,)
            size = len(entry)
            if size >= 1 and size <= 3:
                if size == 3:
                    name, alias, discard = entry
                elif size == 2:
                    (name, alias), discard = entry, False
                elif size == 1:
                    name = alias = entry[0]
                    discard = False
                value = response.get(name)
                if discard and not value:
                    continue
                data[alias] = value
        return data

    def get_scope(self):
        """Return list with needed access scope"""
        return (self.DEFAULT_SCOPE or []) + \
               self.setting('SCOPE', [])

    def get_scope_argument(self):
        param = {}
        scope = self.get_scope()
        if scope:
            param[self.SCOPE_PARAMETER_NAME] = self.SCOPE_SEPARATOR.join(scope)
        return param

    def user_data(self, access_token, *args, **kwargs):
        """Loads user data from service. Implement in subclass"""
        return {}


class BaseOAuth1(OAuthAuth):
    """Consumer based mechanism OAuth authentication, fill the needed
    parameters to communicate properly with authentication service.

        AUTHORIZATION_URL       Authorization service url
        REQUEST_TOKEN_URL       Request token URL
        ACCESS_TOKEN_URL        Access token URL
    """
    AUTHORIZATION_URL = ''
    REQUEST_TOKEN_URL = ''
    REQUEST_TOKEN_METHOD = 'GET'
    OAUTH_TOKEN_PARAMETER_NAME = 'oauth_token'
    REDIRECT_URI_PARAMETER_NAME = 'redirect_uri'
    ACCESS_TOKEN_URL = ''

    def auth_url(self):
        """Return redirect url"""
        token = self.unauthorized_token()
        name = self.name + 'unauthorized_token_name'
        tokens = self.strategy.session_get(name, []) + [token]
        self.strategy.session_set(name, tokens)
        return self.oauth_authorization_request(token)

    def process_error(self, data):
        if 'oauth_problem' in data:
            if data['oauth_problem'] == 'user_refused':
                raise AuthCanceled(self, 'User refused the access')
            raise AuthUnknownError(self, 'Error was ' + data['oauth_problem'])

    def auth_complete(self, *args, **kwargs):
        """Return user, might be logged in"""
        # Multiple unauthorized tokens are supported (see #521)
        self.process_error(self.data)
        name = self.name + 'unauthorized_token_name'
        token = None
        unauthed_tokens = self.strategy.session_get(name, [])
        if not unauthed_tokens:
            raise AuthTokenError(self, 'Missing unauthorized token')
        token_param_name = self.OAUTH_TOKEN_PARAMETER_NAME
        data_token = self.data.get(token_param_name, 'no-token')
        for unauthed_token in unauthed_tokens:
            orig_unauthed_token = unauthed_token
            if not isinstance(unauthed_token, dict):
                unauthed_token = parse_qs(unauthed_token)
            if unauthed_token.get(token_param_name) == data_token:
                self.strategy.session_set(name, list(
                    set(unauthed_tokens) -
                    set([orig_unauthed_token]))
                )
                token = unauthed_token
                break
        else:
            raise AuthTokenError(self, 'Incorrect tokens')

        try:
            access_token = self.access_token(token)
        except HTTPError as err:
            if err.response.status_code == 400:
                raise AuthCanceled(self)
            else:
                raise
        return self.do_auth(access_token, *args, **kwargs)

    def do_auth(self, access_token, *args, **kwargs):
        """Finish the auth process once the access_token was retrieved"""
        data = self.user_data(access_token)
        if data is not None and 'access_token' not in data:
            data['access_token'] = access_token
        kwargs.update({'response': data, 'backend': self})
        return self.strategy.authenticate(*args, **kwargs)

    def unauthorized_token(self):
        """Return request for unauthorized token (first stage)"""
        params = self.request_token_extra_arguments()
        params.update(self.get_scope_argument())
        key, secret = self.get_key_and_secret()
        # decoding='utf-8' produces errors with python-requests on Python3
        # since the final URL will be of type bytes
        decoding = None if six.PY3 else 'utf-8'
        response = self.request(self.REQUEST_TOKEN_URL,
                                params=params,
                                auth=OAuth1(key, secret,
                                            callback_uri=self.redirect_uri,
                                            decoding=decoding),
                                method=self.REQUEST_TOKEN_METHOD)
        return response.content

    def oauth_authorization_request(self, token):
        """Generate OAuth request to authorize token."""
        if not isinstance(token, dict):
            token = parse_qs(token)
        params = self.auth_extra_arguments() or {}
        params.update(self.get_scope_argument())
        params[self.OAUTH_TOKEN_PARAMETER_NAME] = token.get(
            self.OAUTH_TOKEN_PARAMETER_NAME
        )
        params[self.REDIRECT_URI_PARAMETER_NAME] = self.redirect_uri
        return self.AUTHORIZATION_URL + '?' + urlencode(params)

    def oauth_auth(self, token=None, oauth_verifier=None,
                   signature_type=SIGNATURE_TYPE_AUTH_HEADER):
        key, secret = self.get_key_and_secret()
        oauth_verifier = oauth_verifier or self.data.get('oauth_verifier')
        token = token or {}
        # decoding='utf-8' produces errors with python-requests on Python3
        # since the final URL will be of type bytes
        decoding = None if six.PY3 else 'utf-8'
        return OAuth1(key, secret,
                      resource_owner_key=token.get('oauth_token'),
                      resource_owner_secret=token.get('oauth_token_secret'),
                      callback_uri=self.redirect_uri,
                      verifier=oauth_verifier,
                      signature_type=signature_type,
                      decoding=decoding)

    def oauth_request(self, token, url, params=None, method='GET'):
        """Generate OAuth request, setups callback url"""
        return self.request(url, method=method, params=params,
                            auth=self.oauth_auth(token))

    def access_token(self, token):
        """Return request for access token value"""
        return self.get_querystring(self.ACCESS_TOKEN_URL,
                                    auth=self.oauth_auth(token),
                                    method=self.ACCESS_TOKEN_METHOD)


class BaseOAuth2(OAuthAuth):
    """Base class for OAuth2 providers.

    OAuth2 draft details at:
        http://tools.ietf.org/html/draft-ietf-oauth-v2-10

    Attributes:
        AUTHORIZATION_URL       Authorization service url
        ACCESS_TOKEN_URL        Token URL
    """
    AUTHORIZATION_URL = None
    ACCESS_TOKEN_URL = None
    REFRESH_TOKEN_URL = None
    REFRESH_TOKEN_METHOD = 'POST'
    REVOKE_TOKEN_URL = None
    REVOKE_TOKEN_METHOD = 'POST'
    RESPONSE_TYPE = 'code'
    REDIRECT_STATE = True
    STATE_PARAMETER = True

    def state_token(self):
        """Generate csrf token to include as state parameter."""
        return self.strategy.random_string(32)

    def get_redirect_uri(self, state=None):
        """Build redirect with redirect_state parameter."""
        uri = self.redirect_uri
        if self.REDIRECT_STATE and state:
            uri = url_add_parameters(uri, {'redirect_state': state})
        return uri

    def auth_params(self, state=None):
        client_id, client_secret = self.get_key_and_secret()
        params = {
            'client_id': client_id,
            'redirect_uri': self.get_redirect_uri(state)
        }
        if self.STATE_PARAMETER and state:
            params['state'] = state
        if self.RESPONSE_TYPE:
            params['response_type'] = self.RESPONSE_TYPE
        return params

    def auth_url(self):
        """Return redirect url"""
        if self.STATE_PARAMETER or self.REDIRECT_STATE:
            # Store state in session for further request validation. The state
            # value is passed as state parameter (as specified in OAuth2 spec),
            # but also added to redirect, that way we can still verify the
            # request if the provider doesn't implement the state parameter.
            # Reuse token if any.
            name = self.name + '_state'
            state = self.strategy.session_get(name)
            if state is None:
                state = self.state_token()
                self.strategy.session_set(name, state)
        else:
            state = None

        params = self.auth_params(state)
        params.update(self.get_scope_argument())
        params.update(self.auth_extra_arguments())
        params = urlencode(params)
        if not self.REDIRECT_STATE:
            # redirect_uri matching is strictly enforced, so match the
            # providers value exactly.
            params = unquote(params)
        return self.AUTHORIZATION_URL + '?' + params

    def validate_state(self):
        """Validate state value. Raises exception on error, returns state
        value if valid."""
        if not self.STATE_PARAMETER and not self.REDIRECT_STATE:
            return None
        state = self.strategy.session_get(self.name + '_state')
        if state:
            request_state = self.data.get('state') or \
                            self.data.get('redirect_state')
            if not request_state:
                raise AuthMissingParameter(self, 'state')
            elif not state:
                raise AuthStateMissing(self, 'state')
            elif not request_state == state:
                raise AuthStateForbidden(self)
        return state

    def auth_complete_params(self, state=None):
        client_id, client_secret = self.get_key_and_secret()
        return {
            'grant_type': 'authorization_code',  # request auth code
            'code': self.data.get('code', ''),  # server response code
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': self.get_redirect_uri(state)
        }

    def auth_headers(self):
        return {'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'application/json'}

    def request_access_token(self, *args, **kwargs):
        return self.get_json(*args, **kwargs)

    def process_error(self, data):
        if data.get('error'):
            if data['error'] == 'denied' or data['error'] == 'access_denied':
                raise AuthCanceled(self, data.get('error_description', ''))
            raise AuthFailed(self, data.get('error_description') or
                                   data['error'])
        elif 'denied' in data:
            raise AuthCanceled(self, data['denied'])

    def auth_complete(self, *args, **kwargs):
        """Completes loging process, must return user instance"""
        self.process_error(self.data)
        try:
            response = self.request_access_token(
                self.ACCESS_TOKEN_URL,
                data=self.auth_complete_params(self.validate_state()),
                headers=self.auth_headers(),
                method=self.ACCESS_TOKEN_METHOD
            )
        except HTTPError as err:
            if err.response.status_code == 400:
                raise AuthCanceled(self)
            else:
                raise
        except KeyError:
            raise AuthUnknownError(self)
        self.process_error(response)
        return self.do_auth(response['access_token'], response=response,
                            *args, **kwargs)

    def do_auth(self, access_token, *args, **kwargs):
        """Finish the auth process once the access_token was retrieved"""
        data = self.user_data(access_token, *args, **kwargs)
        response = kwargs.get('response') or {}
        response.update(data or {})
        kwargs.update({'response': response, 'backend': self})
        return self.strategy.authenticate(*args, **kwargs)

    def refresh_token_params(self, token, *args, **kwargs):
        client_id, client_secret = self.get_key_and_secret()
        return {
            'refresh_token': token,
            'grant_type': 'refresh_token',
            'client_id': client_id,
            'client_secret': client_secret
        }

    def process_refresh_token_response(self, response, *args, **kwargs):
        return response.json()

    def refresh_token(self, token, *args, **kwargs):
        params = self.refresh_token_params(token, *args, **kwargs)
        request = self.request(self.REFRESH_TOKEN_URL or self.ACCESS_TOKEN_URL,
                               params=params, headers=self.auth_headers(),
                               method=self.REFRESH_TOKEN_METHOD)
        return self.process_refresh_token_response(request, *args, **kwargs)

    def revoke_token_url(self, token, uid):
        return self.REVOKE_TOKEN_URL

    def revoke_token_params(self, token, uid):
        return {}

    def revoke_token_headers(self, token, uid):
        return {}

    def process_revoke_token_response(self, response):
        return response.status_code == 200

    def revoke_token(self, token, uid):
        if self.REVOKE_TOKEN_URL:
            url = self.revoke_token_url(token, uid)
            params = self.revoke_token_params(token, uid)
            headers = self.revoke_token_headers(token, uid)
            data = urlencode(params) if self.REVOKE_TOKEN_METHOD != 'GET' \
                                     else None
            response = self.request(url, params=params, headers=headers,
                                    data=data, method=self.REVOKE_TOKEN_METHOD)
            return self.process_revoke_token_response(response)
