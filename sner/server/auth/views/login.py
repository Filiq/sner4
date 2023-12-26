# This file is part of sner4 project governed by MIT license, see the LICENSE.txt file.
"""
auth login/authentication views
"""

from base64 import b64decode, b64encode
from http import HTTPStatus

from authlib.common.errors import AuthlibBaseError
from fido2 import cbor
from fido2.webauthn import AuthenticatorData, CollectedClientData
from flask import current_app, flash, redirect, request, render_template, Response, session, url_for, jsonify, after_this_request
from flask_login import login_user, logout_user
from requests.exceptions import HTTPError
from sqlalchemy import func

from sner.server.auth.core import regenerate_session, redirect_after_login, TOTPImpl, webauthn_credentials
from sner.server.auth.forms import LoginForm, TotpCodeForm, WebauthnLoginForm
from sner.server.auth.models import User
from sner.server.auth.views import blueprint
from sner.server.extensions import login_manager, oauth, webauthn
from sner.server.forms import ButtonForm
from sner.server.password_supervisor import PasswordSupervisor as PWS


@blueprint.route('/login', methods=['GET', 'POST'])
def login_route():
    """login route"""

    form = LoginForm()

    if form.validate_on_submit():
        user = User.query.filter(User.active, User.username == form.username.data).one_or_none()
        if user:
            if form.password.data:
                if PWS.compare(PWS.hash(form.password.data, PWS.get_salt(user.password)), user.password):
                    if user.totp:
                        session['totp_login_user_id'] = user.id
                        return redirect(url_for('auth.login_totp_route', **request.args))

                    regenerate_session()
                    login_user(user)
                    current_app.logger.info('auth.login password')

                    return jsonify({
                        "id": user.id,
                        "username": user.username,
                        "email": user.email,
                        "roles": user.roles
                    })
            else: 
                if user.webauthn_credentials:
                    session['webauthn_login_user_id'] = user.id
                    return redirect(url_for('auth.login_webauthn_route', **request.args))

        return jsonify({"error": {
            "code": 401,
            "message": "Invalid credentials."
        }}), HTTPStatus.UNAUTHORIZED


@blueprint.route('/logout')
def logout_route():
    """logout route"""

    current_app.logger.info('auth.logout')
    logout_user()
    session.clear()
    
    return jsonify({"message": "Successfully logged out."})


@blueprint.route('/login_totp', methods=['GET', 'POST'])
def login_totp_route():
    """login totp route"""

    user = User.query.filter(User.active, User.id == session.get('totp_login_user_id')).one_or_none()
    if not user:
        return login_manager.unauthorized()

    form = TotpCodeForm()
    if form.validate_on_submit():
        if TOTPImpl(user.totp).verify_code(form.code.data):
            regenerate_session()
            login_user(user)
            current_app.logger.info('auth.login totp')
            return redirect_after_login()

        form.code.errors.append('Invalid code')

    return render_template('auth/login_totp.html', form=form)


@blueprint.route('/login_webauthn_pkcro', methods=['POST'])
def login_webauthn_pkcro_route():
    """login webauthn pkcro route"""

    user = User.query.filter(User.active, User.id == session.get('webauthn_login_user_id')).one_or_none()
    form = ButtonForm()
    if user and form.validate_on_submit():
        pkcro, state = webauthn.authenticate_begin(webauthn_credentials(user))
        session['webauthn_login_state'] = state
        return Response(b64encode(cbor.encode(pkcro)).decode('utf-8'), mimetype='text/plain')

    return '', HTTPStatus.BAD_REQUEST


@blueprint.route('/login_webauthn', methods=['GET', 'POST'])
def login_webauthn_route():
    """login webauthn route"""

    user = User.query.filter(User.active, User.id == session.get('webauthn_login_user_id')).one_or_none()
    if not user:
        return login_manager.unauthorized()

    form = WebauthnLoginForm()
    if form.validate_on_submit():
        try:
            assertion = cbor.decode(b64decode(form.assertion.data))
            webauthn.authenticate_complete(
                session.pop('webauthn_login_state'),
                webauthn_credentials(user),
                assertion['credentialRawId'],
                CollectedClientData(assertion['clientDataJSON']),
                AuthenticatorData(assertion['authenticatorData']),
                assertion['signature'])
            regenerate_session()
            login_user(user)
            current_app.logger.info('auth.login webauthn')
            return redirect_after_login()

        except (KeyError, ValueError) as exc:
            current_app.logger.exception(exc)
            flash('Login error during Webauthn authentication.', 'error')

    return render_template('auth/login_webauthn.html', form=form)


@blueprint.route('/login_oidc')
def login_oidc_route():
    """login oidc"""

    if not current_app.config['OIDC_NAME']:
        flash('OIDC not enabled', 'error')
        return redirect(url_for('auth.login_route'))

    redirect_uri = current_app.config.get(
        f'{current_app.config["OIDC_NAME"]}_REDIRECT_URI',
        url_for('auth.login_oidc_callback_route', _external=True)
    )
    try:
        return getattr(oauth, current_app.config['OIDC_NAME']).authorize_redirect(redirect_uri)
    except (HTTPError, AuthlibBaseError) as exc:
        current_app.logger.exception(exc)
        flash('OIDC Authentication error', 'error')
    return redirect(url_for('auth.login_route'))


@blueprint.route('/login_oidc_callback')
def login_oidc_callback_route():
    """login oidc callback"""

    if not current_app.config['OIDC_NAME']:
        flash('OIDC not enabled', 'error')
        return redirect(url_for('auth.login_route'))

    try:
        token = getattr(oauth, current_app.config['OIDC_NAME']).authorize_access_token()
        userinfo = token.get('userinfo')
    except (HTTPError, AuthlibBaseError) as exc:
        current_app.logger.exception(exc)
        flash('OIDC Authentication error', 'error')
        return redirect(url_for('auth.login_route'))

    if userinfo and userinfo.get('email'):
        user = User.query.filter(User.active, func.lower(User.email) == userinfo.get('email').lower()).one_or_none()
        if user:
            regenerate_session()
            login_user(user)
            current_app.logger.info('auth.login oidc')
            return redirect(url_for('index_route'))

    flash('OIDC Authentication failed', 'error')
    return redirect(url_for('auth.login_route'))
