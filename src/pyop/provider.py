import copy
import functools
import logging
import time
from urllib.parse import parse_qsl
from urllib.parse import urlparse

from jwkest import jws
from oic.oauth2.message import MissingRequiredAttribute
from oic.oauth2.message import ErrorResponse
from oic.oauth2.message import MissingRequiredValue
from oic.oic import scope2claims
from oic.oic.message import AccessTokenRequest
from oic.oic.message import AccessTokenResponse
from oic.oic.message import AuthorizationRequest
from oic.oic.message import AuthorizationResponse
from oic.oic.message import IdToken
from oic.oic.message import OpenIDSchema
from oic.oic.message import RefreshAccessTokenRequest

from .access_token import extract_bearer_token_from_http_request, BearerTokenError
from .client_authentication import verify_client_authentication

logger = logging.getLogger(__name__)


class InvalidAuthenticationRequest(ValueError):
    def __init__(self, message, parsed_request, oauth_error=None):
        super().__init__(message)
        self.request = parsed_request
        self.oauth_error = oauth_error

    def to_error_url(self):
        redirect_uri = self.request.get('redirect_uri')
        if redirect_uri and self.oauth_error:
            error_resp = ErrorResponse(error=self.oauth_error, error_message=str(self))
            return error_resp.request(redirect_uri, should_fragment_encode(self.request))

        return None


class AuthorizationError(Exception):
    pass


class InvalidTokenRequest(ValueError):
    def __init__(self, message, oauth_error='invalid_request'):
        super().__init__(message)
        self.oauth_error = oauth_error


class InvalidUserinfoRequest(ValueError):
    pass


def should_fragment_encode(authentication_request):
    if authentication_request['response_type'] == ['code']:
        # Authorization Code Flow -> query encode
        return False

    return True


def _authorization_request_verify(authentication_request):
    """
    Verifies that all required parameters and correct values are included in the authentication request.
    :param authentication_request: the authentication request to verify
    :raise InvalidAuthenticationRequest: if the authentication is incorrect
    """
    try:
        authentication_request.verify()
    except (MissingRequiredValue, MissingRequiredAttribute) as e:
        raise InvalidAuthenticationRequest(str(e), authentication_request, oauth_error='invalid_request') from e


def _client_id_is_known(provider, authentication_request):
    """
    Verifies the client identifier is known.
    :param provider: provider instance
    :param authentication_request: the authentication request to verify
    :raise InvalidAuthenticationRequest: if the client_id is unknown
    """
    if authentication_request['client_id'] not in provider.clients:
        raise InvalidAuthenticationRequest('Unknown client_id \'{}\''.format(authentication_request['client_id']),
                                           authentication_request,
                                           oauth_error='unauthorized_client')


def _redirect_uri_is_in_registered_redirect_uris(provider, authentication_request):
    """
    Verifies the redirect uri is registered for the client making the request.
    :param provider: provider instance
    :param authentication_request: authentication request to verify
    :raise InvalidAuthenticationRequest: if the redirect uri is not registered
    """
    error = InvalidAuthenticationRequest('Redirect uri \'{}\' is not registered'.format(
        authentication_request['redirect_uri']),
        authentication_request)
    try:
        allowed_redirect_uris = provider.clients[authentication_request['client_id']]['redirect_uris']
    except KeyError as e:
        logger.error('client metadata is missing redirect_uris')
        raise error

    if authentication_request['redirect_uri'] not in allowed_redirect_uris:
        raise error


def _response_type_is_in_registered_response_types(provider, authentication_request):
    """
    Verifies that the requested response type is allowed for the client making the request.
    :param provider: provider instance
    :param authentication_request: authentication request to verify
    :raise InvalidAuthenticationRequest: if the response type is not allowed
    """
    error = InvalidAuthenticationRequest('Response type \'{}\' is not registered'.format(
        ' '.join(authentication_request['response_type'])),
        authentication_request, oauth_error='invalid_request')
    try:
        allowed_response_types = provider.clients[authentication_request['client_id']]['response_types']
    except KeyError as e:
        logger.error('client metadata is missing response_types')
        raise error

    if frozenset(authentication_request['response_type']) not in {frozenset(rt) for rt in allowed_response_types}:
        raise error


def _userinfo_claims_only_specified_when_access_token_is_issued(authentication_request):
    """
    According to <a href="http://openid.net/specs/openid-connect-core-1_0.html#ClaimsParameter">
    "OpenID Connect Core 1.0", Section 5.5</a>: "When the userinfo member is used, the request MUST
    also use a response_type value that results in an Access Token being issued to the Client for
    use at the UserInfo Endpoint."
    :param authentication_request: the authentication request to verify
    :raise InvalidAuthenticationRequest: if the requested claims can not be returned according to the request
    """
    will_issue_access_token = authentication_request['response_type'] != ['id_token']
    contains_userinfo_claims_request = 'claims' in authentication_request and 'userinfo' in authentication_request[
        'claims']
    if not will_issue_access_token and contains_userinfo_claims_request:
        raise InvalidAuthenticationRequest('Userinfo claims cannot be requested, when response_type=\'id_token\'',
                                           authentication_request,
                                           oauth_error='invalid_request')


def _requested_scope_is_supported(provider, authentication_request):
    requested_scopes = set(authentication_request['scope'])
    supported_scopes = set(provider.provider_configuration['scopes_supported'])
    requested_unsupported_scopes = requested_scopes - supported_scopes
    if requested_unsupported_scopes:
        raise InvalidAuthenticationRequest('Request contains unsupported/unknown scopes: {}'
                                           .format(', '.join(requested_unsupported_scopes)),
                                           authentication_request, oauth_error='invalid_scope')


class Provider(object):
    def __init__(self, signing_key, configuration_information, authz_state, clients, userinfo, *,
                 id_token_lifetime=3600):
        # type: (jwkest.jwk.Key, Dict[str, Union[str, Sequence[str]]], se_leg_op.authz_state.AuthorizationState,
        #        Mapping[str, Mapping[str, Any]], se_leg_op.userinfo.Userinfo, int) -> None
        """
        Creates a new provider instance.
        :param configuration_information: see
            <a href="https://openid.net/specs/openid-connect-discovery-1_0.html#ProviderMetadata">
            "OpenID Connect Discovery 1.0", Section 3</a>
        :param clients: see <a href="https://openid.net/specs/openid-connect-registration-1_0.html#ClientMetadata">
            "OpenID Connect Dynamic Client Registration 1.0", Section 2</a>
        :param userinfo: read-only interface for user info
        :param id_token_lifetime: how long the signed ID Tokens should be valid (in seconds), defaults to 1 hour
        """
        self.signing_key = signing_key
        self.configuration_information = configuration_information
        if 'subject_types_supported' not in configuration_information:
            self.configuration_information['subject_types_supported'] = ['pairwise']
        if 'id_token_signing_alg_values_supported' not in configuration_information:
            self.configuration_information['id_token_signing_alg_values_supported'] = ['RS256']
        if 'scopes_supported' not in configuration_information:
            self.configuration_information['scopes_supported'] = ['openid']

        self.authz_state = authz_state
        self.clients = clients
        self.userinfo = userinfo
        self.id_token_lifetime = id_token_lifetime

        self.authentication_request_validators = []  # type: List[Callable[[oic.oic.message.AuthorizationRequest], Boolean]]

        self.authentication_request_validators.append(_authorization_request_verify)
        self.authentication_request_validators.append(
                functools.partial(_client_id_is_known, self))
        self.authentication_request_validators.append(
                functools.partial(_redirect_uri_is_in_registered_redirect_uris, self))
        self.authentication_request_validators.append(
                functools.partial(_response_type_is_in_registered_response_types, self))
        self.authentication_request_validators.append(_userinfo_claims_only_specified_when_access_token_is_issued)
        self.authentication_request_validators.append(functools.partial(_requested_scope_is_supported, self))

    @property
    def provider_configuration(self):
        """
        The provider configuration information.
        """
        return copy.deepcopy(self.configuration_information)

    @property
    def jwks(self):
        """
        All keys published by the provider as JSON Web Key Set.
        """

        keys = [self.signing_key.serialize()]
        return {'keys': keys}

    def parse_authentication_request(self, request_body, http_headers=None):
        # type: (str, Optional[Mapping[str, str]]) -> oic.oic.message.AuthorizationRequest
        """
        Parses and verifies an authentication request.

        :param request_body: urlencoded authentication request
        :param http_headers: http headers
        """

        auth_req = AuthorizationRequest().deserialize(request_body)

        for validator in self.authentication_request_validators:
            validator(auth_req)

        logger.debug('parsed authentication_request: %s', auth_req)
        return auth_req

    def authorize(self, authentication_request, # type: oic.oic.message.AuthorizationRequest
                  user_id, # type: str
                  extra_id_token_claims=None # type: Optional[Union[Mapping[str, Union[str, List[str]]], Callable[[str, str], Mapping[str, Union[str, List[str]]]]]
                  ):
        # type: (...) -> oic.oic.message.AuthorizationResponse
        """
        Creates an Authentication Response for the specified authentication request and local identifier of the
        authenticated user.
        """
        sub = self._create_subject_identifier(user_id, authentication_request['client_id'],
                                              authentication_request['redirect_uri'])
        self._check_subject_identifier_matches_requested(authentication_request, sub)
        response = AuthorizationResponse()

        authz_code = None
        if 'code' in authentication_request['response_type']:
            authz_code = self.authz_state.create_authorization_code(authentication_request, sub)
            response['code'] = authz_code

        access_token_value = None
        if 'token' in authentication_request['response_type']:
            access_token = self.authz_state.create_access_token(authentication_request, sub)
            access_token_value = access_token.value
            self._add_access_token_to_response(response, access_token)

        if 'id_token' in authentication_request['response_type']:
            if extra_id_token_claims is None:
                extra_id_token_claims = {}
            elif callable(extra_id_token_claims):
                extra_id_token_claims = extra_id_token_claims(user_id, authentication_request['client_id'])

            requested_claims = self._get_requested_claims_in(authentication_request, 'id_token')
            if len(authentication_request['response_type']) == 1:
                # only id token is issued -> no way of doing userinfo request, so include all claims in ID Token,
                # even those requested by the scope parameter
                requested_claims.update(scope2claims(authentication_request['scope']))

            user_claims = self.userinfo.get_claims_for(user_id, requested_claims)
            response['id_token'] = self._create_signed_id_token(authentication_request['client_id'], sub,
                                                                user_claims,
                                                                authentication_request.get('nonce'),
                                                                authz_code, access_token_value, extra_id_token_claims)
            logger.debug('issued id_token=%s from requested_claims=%s userinfo=%s extra_claims=%s',
                         response['id_token'], requested_claims, user_claims, extra_id_token_claims)

        if 'state' in authentication_request:
            response['state'] = authentication_request['state']
        return response

    def _add_access_token_to_response(self, response, access_token):
        # type: (oic.message.AccessTokenResponse, se_leg_op.access_token.AccessToken) -> None
        """
        Adds the Access Token and the associated parameters to the Token Response.
        """
        response['access_token'] = access_token.value
        response['token_type'] = access_token.type
        response['expires_in'] = access_token.expires_in

    def _create_subject_identifier(self, user_id, client_id, redirect_uri):
        # type (str, str, str) -> str
        """
        Creates a subject identifier for the specified client and user
        see <a href="http://openid.net/specs/openid-connect-core-1_0.html#Terminology">
        "OpenID Connect Core 1.0", Section 1.2</a>.
        :param user_id: local user identifier
        :param client_id: which client to generate a subject identifier for
        :param redirect_uri: the clients' redirect_uri
        :return: a subject identifier for the user intended for client who made the authentication request
        """
        supported_subject_types = self.configuration_information['subject_types_supported'][0]
        subject_type = self.clients[client_id].get('subject_type', supported_subject_types)
        sector_identifier = urlparse(redirect_uri).netloc
        return self.authz_state.get_subject_identifier(subject_type, user_id, sector_identifier)

    def _get_requested_claims_in(self, authentication_request, response_method):
        # type (oic.oic.message.AuthorizationRequest, str) -> Mapping[str, Optional[Mapping[str, Union[str, List[str]]]]
        """
        Parses any claims requested using the 'claims' request parameter, see
        <a href="http://openid.net/specs/openid-connect-core-1_0.html#ClaimsParameter">
        "OpenID Connect Core 1.0", Section 5.5</a>.
        :param authentication_request: the authentication request
        :param response_method: 'id_token' or 'userinfo'
        """
        if response_method != 'id_token' and response_method != 'userinfo':
            raise ValueError('response_method must be \'id_token\' or \'userinfo\'')

        requested_claims = {}

        if 'claims' in authentication_request and response_method in authentication_request['claims']:
            requested_claims.update(authentication_request['claims'][response_method])
        return requested_claims

    def _create_signed_id_token(self,
                                client_id,  # type: str
                                sub,  # type: str
                                user_claims=None,  # type: Optional[Mapping[str, Union[str, List[str]]]]
                                nonce=None,  # type: Optional[str]
                                authorization_code=None,  # type: Optional[str]
                                access_token_value=None,  # type: Optional[str]
                                extra_id_token_claims=None):  # type: Optional[Mappings[str, Union[str, List[str]]]]
        # type: (...) -> str
        """
        Creates a signed ID Token.
        :param client_id: who the ID Token is intended for
        :param sub: who the ID Token is regarding
        :param user_claims: any claims about the user to be included
        :param nonce: nonce from the authentication request
        :param authorization_code: the authorization code issued together with this ID Token
        :param access_token_value: the access token issued together with this ID Token
        :param extra_id_token_claims: any extra claims that should be included in the ID Token
        :return: a JWS, containing the ID Token as payload
        """

        alg = self.clients[client_id].get('id_token_signed_response_alg',
                                          self.configuration_information['id_token_signing_alg_values_supported'][0])
        args = {}

        hash_alg = 'HS{}'.format(alg[-3:])
        if authorization_code:
            args['c_hash'] = jws.left_hash(authorization_code.encode('utf-8'), hash_alg)
        if access_token_value:
            args['at_hash'] = jws.left_hash(access_token_value.encode('utf-8'), hash_alg)

        if user_claims:
            args.update(user_claims)

        if extra_id_token_claims:
            args.update(extra_id_token_claims)

        id_token = IdToken(iss=self.configuration_information['issuer'],
                           sub=sub,
                           aud=client_id,
                           iat=time.time(),
                           exp=time.time() + self.id_token_lifetime,
                           **args)

        if nonce:
            id_token['nonce'] = nonce

        logger.debug('signed id_token with kid=%s using alg=%s', self.signing_key, alg)
        return id_token.to_jwt([self.signing_key], alg)

    def _check_subject_identifier_matches_requested(self, authentication_request, sub):
        # type (oic.message.AuthorizationRequest, str) -> None
        """
        Verifies the subject identifier against any requested subject identifier using the claims request parameter.
        :param authentication_request: authentication request
        :param sub: subject identifier
        :raise AuthorizationError: if the subject identifier does not match the requested one
        """
        if 'claims' in authentication_request:
            requested_id_token_sub = authentication_request['claims'].get('id_token', {}).get('sub')
            requested_userinfo_sub = authentication_request['claims'].get('userinfo', {}).get('sub')
            if requested_id_token_sub and requested_userinfo_sub and requested_id_token_sub != requested_userinfo_sub:
                raise AuthorizationError('Requested different subject identifier for IDToken and userinfo: {} != {}'
                                         .format(requested_id_token_sub, requested_userinfo_sub))

            requested_sub = requested_id_token_sub or requested_userinfo_sub
            if requested_sub and sub != requested_sub:
                raise AuthorizationError('Requested subject identifier \'{}\' could not be matched'
                                         .format(requested_sub))

    def handle_token_request(self, request_body, # type: str
                             http_headers=None, # type: Optional[Mapping[str, str]]
                             extra_id_token_claims=None # type: Optional[Union[Mapping[str, Union[str, List[str]]], Callable[[str, str], Mapping[str, Union[str, List[str]]]]]
                             ):
        # type: (...) -> oic.oic.message.AccessTokenResponse
        """
        Handles a token request, either for exchanging an authorization code or using a refresh token.
        :param request_body: urlencoded token request
        :param http_headers: http headers
        :param extra_id_token_claims: extra claims to include in the signed ID Token
        """

        token_request = self._verify_client_authentication(request_body, http_headers)

        if 'grant_type' not in token_request:
            raise InvalidTokenRequest('grant_type missing')
        elif token_request['grant_type'] == 'authorization_code':
            return self._do_code_exchange(token_request, extra_id_token_claims)
        elif token_request['grant_type'] == 'refresh_token':
            return self._do_token_refresh(token_request)

        raise InvalidTokenRequest('grant_type \'{}\' unknown'.format(token_request['grant_type']),
                                  oauth_error='unsupported_grant_type')

    def _do_code_exchange(self, request, # type: Dict[str, str]
                          extra_id_token_claims=None # type: Optional[Union[Mapping[str, Union[str, List[str]]], Callable[[str, str], Mapping[str, Union[str, List[str]]]]]
                          ):
        # type: (...) -> oic.message.AccessTokenResponse
        """
        Handles a token request for exchanging an authorization code for an access token
        (grant_type=authorization_code).
        :param request: parsed http request parameters
        :param extra_id_token_claims: any extra parameters to include in the signed ID Token, either as a dict-like
            object or as a callable object accepting the local user identifier and client identifier which returns
            any extra claims which might depend on the user id and/or client id.
        :return: a token response containing a signed ID Token, an Access Token, and a Refresh Token
        :raise InvalidTokenRequest: if the token request is invalid
        """
        token_request = AccessTokenRequest().from_dict(request)
        try:
            token_request.verify()
        except (MissingRequiredValue, MissingRequiredAttribute) as e:
            raise InvalidTokenRequest(str(e)) from e

        authentication_request = self.authz_state.get_authorization_request_for_code(token_request['code'])

        if token_request['redirect_uri'] != authentication_request['redirect_uri']:
            raise InvalidTokenRequest('Invalid redirect_uri: {} != {}'.format(token_request['redirect_uri'],
                                                                              authentication_request['redirect_uri']))

        sub = self.authz_state.get_subject_identifier_for_code(token_request['code'])
        user_id = self.authz_state.get_user_id_for_subject_identifier(sub)

        response = AccessTokenResponse()

        access_token = self.authz_state.exchange_code_for_token(token_request['code'])
        self._add_access_token_to_response(response, access_token)
        response['refresh_token'] = self.authz_state.create_refresh_token(access_token.value)

        if extra_id_token_claims is None:
            extra_id_token_claims = {}
        elif callable(extra_id_token_claims):
            extra_id_token_claims = extra_id_token_claims(user_id, authentication_request['client_id'])
        requested_claims = self._get_requested_claims_in(authentication_request, 'id_token')
        user_claims = self.userinfo.get_claims_for(user_id, requested_claims)
        response['id_token'] = self._create_signed_id_token(authentication_request['client_id'], sub,
                                                            user_claims,
                                                            authentication_request.get('nonce'),
                                                            None, access_token.value,
                                                            extra_id_token_claims)
        logger.debug('issued id_token=%s from requested_claims=%s userinfo=%s extra_claims=%s',
                     response['id_token'], requested_claims, user_claims, extra_id_token_claims)

        return response

    def _do_token_refresh(self, request):
        # type: (Mapping[str, str]) -> oic.oic.message.AccessTokenResponse
        """
        Handles a token request for refreshing an access token (grant_type=refresh_token).
        :param request: parsed http request parameters
        :return: a token response containing a new Access Token and possibly a new Refresh Token
        :raise InvalidTokenRequest: if the token request is invalid
        """
        token_request = RefreshAccessTokenRequest().from_dict(request)
        try:
            token_request.verify()
        except (MissingRequiredValue, MissingRequiredAttribute) as e:
            raise InvalidTokenRequest(str(e)) from e

        response = AccessTokenResponse()

        access_token, refresh_token = self.authz_state.use_refresh_token(token_request['refresh_token'],
                                                                         scope=token_request.get('scope'))
        self._add_access_token_to_response(response, access_token)
        if refresh_token:
            response['refresh_token'] = refresh_token

        return response

    def _verify_client_authentication(self, request_body, http_headers=None):
        # type (str, Optional[Mapping[str, str]] -> Mapping[str, str]
        """
        Verifies the client authentication.
        :param request_body: urlencoded token request
        :param http_headers:
        :return: The parsed request body.
        """
        if http_headers is None:
            http_headers = {}
        token_request = dict(parse_qsl(request_body))
        verify_client_authentication(token_request, self.clients, http_headers.get('Authorization'))
        return token_request

    def handle_userinfo_request(self, request=None, http_headers=None):
        # type: (Optional[str], Optional[Mapping[str, str]]) -> oic.oic.message.OpenIDSchema
        """
        Handles a userinfo request.
        :param request: urlencoded request (either query string or POST body)
        :param http_headers: http headers
        """
        bearer_token = extract_bearer_token_from_http_request(request, http_headers)

        introspection = self.authz_state.introspect_access_token(bearer_token)
        if not introspection['active']:
            raise InvalidUserinfoRequest('The access token has expired')
        scope = introspection['scope']
        user_id = self.authz_state.get_user_id_for_subject_identifier(introspection['sub'])

        requested_claims = scope2claims(scope.split())
        authentication_request = self.authz_state.get_authorization_request_for_access_token(bearer_token)
        requested_claims.update(self._get_requested_claims_in(authentication_request, 'userinfo'))
        user_claims = self.userinfo.get_claims_for(user_id, requested_claims)

        response = OpenIDSchema(sub=introspection['sub'], **user_claims)
        logger.debug('userinfo=%s from requested_claims=%s userinfo=%s extra_claims=%s',
                     OpenIDSchema(**user_claims), requested_claims, user_claims)
        return response
