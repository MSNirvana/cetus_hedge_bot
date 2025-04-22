import ccxt
import yaml
import time
import math
from pysui import SuiConfig, SyncClient, SuiRpcResult
from pysui.sui.sui_txn import SyncTransaction
from pysui.sui.sui_types import SuiString, SuiU64
from decimal import Decimal
from telegram import Bot
from telegram.constants import ParseMode

class CetusHedgeBot:
    def __init__(self, config_path):
        self.load_config(config_path)
        self.sui_client = SyncClient(SuiConfig.from_config({
            'rpc_url': self.config['cetus']['rpc_url'],
            'priv_key': self.config['cetus']['private_key']
        }))
        self.binance = ccxt.binance({
            'apiKey': self.config['exchanges']['binance']['api_key'],
            'secret': self.config['exchanges']['binance']['api_secret'],
            'options': {'defaultType': 'future'}
        })
        self.tg_bot = Bot(token=self.config['notifications']['telegram']['bot_token'])
        self.current_position = None

    def load_config(self, path):
        with open(path) as f:
            self.config = yaml.safe_load(f)

    def get_sui_price(self):
        """获取实时SUI价格[7](@ref)"""
        ticker = self.binance.fetch_ticker('SUI/USDC')
        return float(ticker['last'])

    def calculate_swap_amounts(self):
        """计算兑换比例"""
        current_price = self.get_sui_price()
        total_usdc = self.config['strategy']['base_amount']
        # 按当前价格分配SUI/USDC
        sui_amount = (total_usdc * 0.5) / current_price  # 50%兑换SUI
        usdc_amount = total_usdc * 0.5
        return sui_amount, usdc_amount

    def add_liquidity(self, sui_amount, usdc_amount, lower_tick, upper_tick):
        """添加流动性到Cetus V3池[8](@ref)"""
        txer = SyncTransaction(self.sui_client)
        # 转换精度
        sui_scaled = int(sui_amount * 10**self.config['tokens']['sui']['decimals'])
        usdc_scaled = int(usdc_amount * 10**self.config['tokens']['usdc']['decimals'])
        
        # 调用Cetus合约
        txer.move_call(
            target=f"{self.config['cetus']['pool_address']}::pool::add_liquidity",
            arguments=[
                txer.object(self.config['cetus']['pool_address']),
                txer.split_coins(txer.gas, [SuiU64(sui_scaled)]),
                txer.split_coins(txer.gas, [SuiU64(usdc_scaled)]),
                SuiU64(lower_tick),
                SuiU64(upper_tick),
                txer.pure(0) # 滑点容忍度
            ],
            type_arguments=[
                self.config['tokens']['sui']['type'],
                self.config['tokens']['usdc']['type']
            ]
        )
        result = txer.execute()
        if result.is_ok():
            return result.result_data
        else:
            raise Exception(f"添加流动性失败: {result.result_string}")

    def execute_hedge(self, sui_amount):
        """执行币安空单对冲[3](@ref)"""
        try:
            order = self.binance.create_market_sell_order(
                symbol='SUI/USDC',
                amount=sui_amount,
                params={'positionSide': 'SHORT'}
            )
            return order['id']
        except Exception as e:
            self.send_alert(f"对冲订单失败: {str(e)}")

    def check_rebalance_condition(self):
        """检查调仓条件[6](@ref)"""
        current_price = self.get_sui_price()
        lower_bound = self.current_position['lower_price']
        upper_bound = self.current_position['upper_price']
        
        # 计算偏离程度
        lower_threshold = lower_bound * (1 + self.config['strategy']['rebalance_threshold'])
        upper_threshold = upper_bound * (1 - self.config['strategy']['rebalance_threshold'])
        
        return current_price <= lower_threshold or current_price >= upper_threshold

    def rebalance_position(self):
        """执行调仓操作"""
        # 1. 撤回流動性
        self.remove_liquidity()
        
        # 2. 重新计算仓位
        current_price = self.get_sui_price()
        new_lower = current_price * (1 - self.config['strategy']['price_range'])
        new_upper = current_price * (1 + self.config['strategy']['price_range'])
        
        # 3. 平仓旧空单
        self.close_hedge_position()
        
        # 4. 新建仓位
        self.initialize_position()

    def generate_report(self):
        """生成监控报告[7](@ref)"""
        position = self.get_pool_position()
        binance_pos = self.binance.fetch_position('SUI/USDC')
        
        msg = f"""
📊 *持仓状态报告*
┌───────────────────┬───────────────┐
│ 当前SUI价格       │ ${self.get_sui_price():.4f}
├───────────────────┼───────────────┤
│ 流动性区间        │ ${position['lower']:.4f} - ${position['upper']:.4f}
├───────────────────┼───────────────┤
│ 偏离区间边缘      │ {abs(position['deviation']):.2%}
├───────────────────┼───────────────┤
│ 币安空头数量      │ {binance_pos['contracts']} SUI
└───────────────────┴───────────────┘
        """
        return msg

    def main_loop(self):
        """主循环逻辑"""
        self.initialize_position()
        while True:
            try:
                if self.check_rebalance_condition():
                    self.rebalance_position()
                
                report = self.generate_report()
                self.tg_bot.send_message(
                    chat_id=self.config['notifications']['telegram']['chat_id'],
                    text=report,
                    parse_mode=ParseMode.MARKDOWN
                )
                
                time.sleep(self.config['strategy']['check_interval'])
            
            except Exception as e:
                self.send_alert(f"系统错误: {str(e)}")
                time.sleep(60)

if __name__ == "__main__":
    bot = CetusHedgeBot('config.yaml')
    bot.main_loop()