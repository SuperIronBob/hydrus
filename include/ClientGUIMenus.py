import ClientConstants as CC
import collections
import wx

menus_to_submenus = collections.defaultdict( set )
menus_to_menu_item_data = collections.defaultdict( set )

def AppendMenu( menu, submenu, label ):
    
    label = SanitiseLabel( label )
    
    menu.AppendMenu( CC.ID_NULL, label, submenu )
    
    menus_to_submenus[ menu ].add( submenu )
    
def AppendMenuBitmapItem( menu, label, description, event_handler, bitmap, callable, *args, **kwargs ):
    
    label = SanitiseLabel( label )
    
    menu_item = wx.MenuItem( menu, wx.ID_ANY, label )
    
    menu_item.SetHelp( description )
    
    menu_item.SetBitmap( bitmap )
    
    menu.AppendItem( menu_item )
    
    BindMenuItem( menu, event_handler, menu_item, callable, *args, **kwargs )
    
    return menu_item
    
def AppendMenuCheckItem( menu, label, description, event_handler, initial_value, callable, *args, **kwargs ):
    
    label = SanitiseLabel( label )
    
    menu_item = menu.AppendCheckItem( wx.ID_ANY, label, description )
    
    menu_item.Check( initial_value )
    
    BindMenuItem( menu, event_handler, menu_item, callable, *args, **kwargs )
    
    return menu_item
    
def AppendMenuItem( menu, label, description, event_handler, callable, *args, **kwargs ):
    
    label = SanitiseLabel( label )
    
    menu_item = menu.Append( wx.ID_ANY, label, description )
    
    BindMenuItem( menu, event_handler, menu_item, callable, *args, **kwargs )
    
    return menu_item
    
def BindMenuItem( menu, event_handler, menu_item, callable, *args, **kwargs ):
    
    l_callable = GetLambdaCallable( callable, *args, **kwargs )
    
    event_handler.Bind( wx.EVT_MENU, l_callable, source = menu_item )
    
    menus_to_menu_item_data[ menu ].add( ( menu_item, event_handler ) )
    
def DestroyMenuItems( menu ):
    
    menu_item_data = menus_to_menu_item_data[ menu ]
    
    del menus_to_menu_item_data[ menu ]
    
    for ( menu_item, event_handler ) in menu_item_data:
        
        event_handler.Unbind( wx.EVT_MENU, source = menu_item )
        
        menu_item.Destroy()
        
    
    submenus = menus_to_submenus[ menu ]
    
    del menus_to_submenus[ menu ]
    
    for submenu in submenus:
        
        DestroyMenuItems( submenu )
        
    
def DestroyMenu( menu ):
    
    DestroyMenuItems( menu )
    
    menu.Destroy()
    
def GetLambdaCallable( callable, *args, **kwargs ):
    
    l_callable = lambda event: callable( *args, **kwargs )
    
    return l_callable
    
def SanitiseLabel( label ):
    
    return label.replace( '&', '&&' )
    