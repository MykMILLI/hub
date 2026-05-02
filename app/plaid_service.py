"""
Plaid Integration Service for Banking Features

Handles OAuth flow, transaction syncing, and account management with Plaid API
"""

try:
    from plaid import ApiClient, Configuration
    from plaid.api import plaid_api
    from plaid.model.country_code import CountryCode
    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
    from plaid.model.transactions_get_request import TransactionsGetRequest
    from plaid.model.accounts_get_request import AccountsGetRequest
    from plaid.model.auth_get_request import AuthGetRequest
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.products import Products
    PLAID_AVAILABLE = True
except ImportError:
    PLAID_AVAILABLE = False

from .models import db, PlaidAccount, PlaidTransaction
from datetime import datetime, timedelta
import os
import logging

logger = logging.getLogger(__name__)

class PlaidService:
    """Service for Plaid API interactions"""

    def __init__(self, app=None):
        self.client = None
        self.config = None
        if app:
            self.init_app(app)

    def init_app(self, app):
        """Initialize Plaid service with Flask app config"""
        # Get Plaid credentials from environment or config
        client_id = os.getenv('PLAID_CLIENT_ID', app.config.get('PLAID_CLIENT_ID', ''))
        secret = os.getenv('PLAID_SECRET', app.config.get('PLAID_SECRET', ''))
        env = os.getenv('PLAID_ENV', app.config.get('PLAID_ENV', 'sandbox'))

        if not client_id or not secret:
            logger.warning("Plaid credentials not configured. Plaid features disabled.")
            return

        # Set up Plaid API client
        configuration = Configuration(
            host=self._get_plaid_host(env),
            api_key={
                'clientId': client_id,
                'secret': secret,
            }
        )
        self.client = plaid_api.PlaidApi(ApiClient(configuration))
        self.config = {
            'client_id': client_id,
            'environment': env,
        }

    def _get_plaid_host(self, env):
        """Get Plaid API host based on environment"""
        hosts = {
            'sandbox': 'https://sandbox.plaid.com',
            'development': 'https://development.plaid.com',
            'production': 'https://production.plaid.com',
        }
        return hosts.get(env, 'https://sandbox.plaid.com')

    def is_configured(self):
        """Check if Plaid is properly configured"""
        return self.client is not None

    def create_link_token(self, user_id: str, redirect_uri: str = None):
        """Create a Plaid Link token for account connection
        
        Args:
            user_id: Family member identifier
            redirect_uri: Redirect URI after successful connection
            
        Returns:
            Link token for frontend integration
        """
        if not self.is_configured():
            raise ValueError("Plaid is not configured")

        try:
            request_data = {
                'products': [Products('transactions')],
                'client_name': 'HomeHub Family Budget',
                'country_codes': [CountryCode('US')],
                'language': 'en',
                'user': {
                    'client_user_id': user_id,
                },
            }
            
            if redirect_uri:
                request_data['redirect_uri'] = redirect_uri

            request = LinkTokenCreateRequest(**request_data)
            response = self.client.link_token_create(request)
            return response.link_token
        except Exception as e:
            logger.error(f"Error creating Plaid link token: {e}")
            raise

    def exchange_public_token(self, public_token: str, user: str):
        """Exchange public token for access token
        
        Args:
            public_token: Token from Plaid Link
            user: Family member identifier
            
        Returns:
            PlaidAccount database entry
        """
        if not self.is_configured():
            raise ValueError("Plaid is not configured")

        try:
            request = ItemPublicTokenExchangeRequest(
                public_token=public_token,
            )
            response = self.client.item_public_token_exchange(request)
            
            access_token = response.access_token
            item_id = response.item_id

            # Fetch account details
            accounts_request = AccountsGetRequest(access_token=access_token)
            accounts_response = self.client.accounts_get(accounts_request)

            # Store each account
            plaid_accounts = []
            for account in accounts_response.accounts:
                plaid_account = PlaidAccount(
                    user=user,
                    access_token=access_token,
                    item_id=item_id,
                    account_id=account.account_id,
                    account_name=account.name,
                    account_type=account.type,
                    account_subtype=account.subtype,
                    balance=account.balances.current,
                    currency=account.balances.iso_currency_code or 'USD',
                    synced_at=datetime.utcnow(),
                )
                db.session.add(plaid_account)
                plaid_accounts.append(plaid_account)

            db.session.commit()
            logger.info(f"Successfully linked {len(plaid_accounts)} accounts for user {user}")
            return plaid_accounts

        except Exception as e:
            logger.error(f"Error exchanging public token: {e}")
            db.session.rollback()
            raise

    def sync_transactions(self, plaid_account_id: int, days_back: int = 30):
        """Sync transactions from Plaid for an account
        
        Args:
            plaid_account_id: Database ID of PlaidAccount
            days_back: Number of days to fetch (default 30)
            
        Returns:
            Number of new transactions synced
        """
        if not self.is_configured():
            return 0

        try:
            plaid_account = PlaidAccount.query.get(plaid_account_id)
            if not plaid_account or not plaid_account.active:
                return 0

            # Calculate date range
            end_date = datetime.now().date()
            start_date = end_date - timedelta(days=days_back)

            # Fetch transactions
            request = TransactionsGetRequest(
                access_token=plaid_account.access_token,
                start_date=start_date,
                end_date=end_date,
                options={
                    'account_ids': [plaid_account.account_id],
                    'count': 250,
                    'offset': 0,
                },
            )

            response = self.client.transactions_get(request)
            transactions = response.transactions

            # Store transactions that don't already exist
            new_count = 0
            for trans in transactions:
                existing = PlaidTransaction.query.filter_by(
                    plaid_transaction_id=trans.transaction_id
                ).first()
                
                if not existing:
                    plaid_trans = PlaidTransaction(
                        plaid_account_id=plaid_account.id,
                        plaid_transaction_id=trans.transaction_id,
                        transaction_date=trans.date,
                        posted_date=trans.posted_date,
                        amount=trans.amount,
                        currency=trans.iso_currency_code or 'USD',
                        description=trans.name,
                        merchant=trans.merchant_name or 'Unknown',
                        category=trans.personal_finance_category.primary if trans.personal_finance_category else None,
                        pending=trans.pending,
                    )
                    db.session.add(plaid_trans)
                    new_count += 1

            # Update sync timestamp
            plaid_account.synced_at = datetime.utcnow()
            db.session.commit()

            logger.info(f"Synced {new_count} new transactions for account {plaid_account_id}")
            return new_count

        except Exception as e:
            logger.error(f"Error syncing transactions: {e}")
            db.session.rollback()
            return 0

    def sync_balances(self, plaid_account_id: int):
        """Sync account balances from Plaid
        
        Args:
            plaid_account_id: Database ID of PlaidAccount
            
        Returns:
            Updated balance or None on error
        """
        if not self.is_configured():
            return None

        try:
            plaid_account = PlaidAccount.query.get(plaid_account_id)
            if not plaid_account or not plaid_account.active:
                return None

            request = AccountsGetRequest(access_token=plaid_account.access_token)
            response = self.client.accounts_get(request)

            for account in response.accounts:
                if account.account_id == plaid_account.account_id:
                    plaid_account.balance = account.balances.current
                    plaid_account.updated_at = datetime.utcnow()
                    db.session.commit()
                    logger.info(f"Updated balance for account {plaid_account_id}: {account.balances.current}")
                    return account.balances.current

            return None

        except Exception as e:
            logger.error(f"Error syncing balances: {e}")
            return None

    def disconnect_account(self, plaid_account_id: int):
        """Disconnect and remove a Plaid account
        
        Args:
            plaid_account_id: Database ID of PlaidAccount
            
        Returns:
            True if successful
        """
        try:
            plaid_account = PlaidAccount.query.get(plaid_account_id)
            if plaid_account:
                plaid_account.active = False
                plaid_account.updated_at = datetime.utcnow()
                
                # Remove associated transactions
                PlaidTransaction.query.filter_by(
                    plaid_account_id=plaid_account_id
                ).delete()
                
                db.session.commit()
                logger.info(f"Disconnected Plaid account {plaid_account_id}")
                return True
            return False

        except Exception as e:
            logger.error(f"Error disconnecting Plaid account: {e}")
            db.session.rollback()
            return False

    def get_account_transactions(self, plaid_account_id: int, start_date=None, end_date=None, limit=100):
        """Get synced transactions for an account
        
        Args:
            plaid_account_id: Database ID of PlaidAccount
            start_date: Filter by start date
            end_date: Filter by end date
            limit: Maximum results
            
        Returns:
            List of transactions
        """
        query = PlaidTransaction.query.filter_by(
            plaid_account_id=plaid_account_id
        ).order_by(PlaidTransaction.transaction_date.desc())

        if start_date:
            query = query.filter(PlaidTransaction.transaction_date >= start_date)
        if end_date:
            query = query.filter(PlaidTransaction.transaction_date <= end_date)

        return query.limit(limit).all()


# Global Plaid service instance
plaid_service = PlaidService()
